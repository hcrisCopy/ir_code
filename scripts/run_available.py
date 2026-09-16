"""一键续跑第二阶段统计：发现数据 → 盘点进度 → 跳过已完成 → 继续未完成 → 汇总。

设计目标（老师的第二阶段要求 → 本脚本的职责）
--------------------------------------------
本脚本**不改统计口径**，口径全部在 stage2.py / _common.py 里。它只负责"编排"：
    1. 发现当前有哪些数据集、哪些 config/子集；
    2. 判断每条配置的就绪度（输入文件齐不齐）与完成度（数量/长度做没做完）；
    3. 打印一张计划表，让人一眼看出"还要跑哪些、为什么";
    4. 跳过已完成、继续未完成、单条失败不中断整轮；
    5. 每条跑完立刻更新进度文件，随时可以 Ctrl-C 后重跑续上；
    6. 结束打印汇总与未完成清单。

用法（在 ir_code/ 下执行）
--------------------------
    python scripts/run_available.py                 # 全自动：跳过已完成，跑剩下的
    python scripts/run_available.py --plan          # 只看计划，不跑
    python scripts/run_available.py --dataset r2med beir        # 只处理这些数据集
    python scripts/run_available.py --config r2med-test         # 只处理这些 config
    python scripts/run_available.py --force         # 忽略已有结果，全部重跑
    python scripts/run_available.py --lengths-only  # 只补长度（数量已完成的）
    python scripts/run_available.py --extract       # 先解压（默认不解压，见下）
    python scripts/run_available.py --limit 200     # 冒烟：每条只跑 200 个样本，写 debug 目录
    python scripts/run_available.py --no-token      # 只算 word，不加载 tokenizer
    python scripts/run_available.py --no-merge      # 不每条都汇总，只在最后汇总一次

为什么默认**不解压**
--------------------
extract_data.py 在核对通过后会**删掉原压缩包**，而有的包解开后是几十 GB 量级
（例如 tripclick 的 dlfiles.tar.gz 28.7 GiB）。在磁盘紧张的服务器上不该由"一键脚本"
擅自决定。所以默认只**提示**哪些数据集看起来需要解压，要解压请显式加 --extract。

退出码：0 表示没有失败项；1 表示有配置失败或仍不可用（细节见 run_status.json）。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import traceback
from collections import OrderedDict
from pathlib import Path

from _common import (CACHE_DIR, IR_CODE, OUTPUTS_DIR, banner, dataset_path,
                     expand_files, hsize, load_manifest, real_files, short,
                     to_display)
from stage2 import (basic, configurations, csv_update, fingerprint, lengths,
                    stem, write_json)

# stage2.basic() 里有一条"降级分支"：这些 kind / usage_scope 的配置不读任何文件，
# 只把 manifest 的声明值写进结果。runner 必须用**同一套判据**识别它们，否则会把
# "本来就只登记声明"的配置当成"还没做完"，每轮都重跑。
# ⚠️ 这份集合与 stage2.basic() 第 338 行保持一致；改了那边要同步改这里。
DECLARED_ONLY_KINDS = {'derived', 'external', 'mteb_task', 'tsv_triples', 'tsv_tuples', None}

# 先跑小而完整的输入，把大训练集留在后面。表外的数据集按 manifest 顺序排在后面。
PRIORITY = (
    'auxiliary_math', 'rearank_12k', 'r2med', 'fullrank_training_data', 'bright',
    'rank_zephyr_training_data', 'reasonrank_data_13k', 'e2rank_ranking_datasets',
    'trec_dl', 'nfcorpus', 'trec_covid', 'mirage', 'pmc_patients_recds',
    'msmarco_passage_v1', 'msmarco_passage_v2', 'msmarco_document_v1',
    'beir', 'mteb_english', 'miriad_4_4m', 'pe_rank_training_data',
    'kilt_wikipedia_passages', 'atlas_wikipedia_2020_12', 'echo_e5_training_data',
    'tripclick',
)

ACTION_ORDER = ('数量+长度', '长度', '数量', '跳过')


def dataset_rank(name: str) -> int:
    return PRIORITY.index(name) if name in PRIORITY else len(PRIORITY)


def is_declared_only(config: dict) -> bool:
    kind = (config.get('sample_source') or {}).get('kind')
    return kind in DECLARED_ONLY_KINDS or config.get('usage_scope') == 'unknown_subset'


def declared_bytes(entry: dict, path: Path) -> int | None:
    """manifest 里为此文件声明的字节数（有才比对）。"""
    base = dataset_path(entry)
    for declaration in entry.get('files', []):
        if 'bytes' not in declaration:
            continue
        if path in expand_files(declaration['path'], base):
            return declaration['bytes']
    return None


def input_state(entry: dict, config: dict) -> tuple[str, str]:
    """输入就绪度：ready / partial / missing / declared_only。

    partial = 文件在但字节数与 manifest 声明不符 —— 典型是上传还没完，
    这种情况 stage2 也会拒绝出正式统计，所以计划表里单列出来提醒等待。
    """
    if is_declared_only(config):
        return 'declared_only', '只登记论文声明，不读文件'
    spec = config.get('sample_source') or {}
    pattern = spec.get('file')
    if not pattern:
        return 'missing', '配置没写 sample_source.file'
    files = expand_files(pattern, dataset_path(entry))
    if not files:
        return 'missing', f'缺输入文件 {pattern}'
    minimum = spec.get('min_files', 1)
    if len(files) < minimum:
        return 'partial', f'只有 {len(files)} / {minimum} 个输入分片'
    for path in files:
        expected = declared_bytes(entry, path)
        if expected is not None and path.stat().st_size != expected:
            return 'partial', f'{path.name} 目前 {hsize(path.stat().st_size)}，应为 {hsize(expected)}'
    return 'ready', f'{len(files)} 个输入文件'


def completion(entry: dict, config: dict, force: bool = False) -> dict:
    """这条配置做到哪一步了。判定逻辑与 stage2 内部保持一致（用同一个 fingerprint）。"""
    name = stem(config)
    basic_path = OUTPUTS_DIR / 'experiments' / f'{name}.json'
    length_path = OUTPUTS_DIR / 'experiments' / f'{name}_lengths.json'
    signature = fingerprint(entry, config)
    basic_result = json.loads(basic_path.read_text(encoding='utf-8')) if basic_path.exists() else {}
    length_result = json.loads(length_path.read_text(encoding='utf-8')) if length_path.exists() else {}
    if force:
        return {'basic': False, 'lengths': False, 'reason': '按 --force 重跑'}
    basic_ok = (basic_result.get('status') == 'complete'
                and basic_result.get('fingerprint') == signature)
    if not basic_ok:
        reason = '还没有数量结果' if not basic_result else f"数量 {basic_result.get('status')}：{short(basic_result.get('notes'), 60)}"
        return {'basic': False, 'lengths': False, 'reason': reason}
    # 数量说 complete，但候选缓存 sqlite 被清掉时，长度会算出一份"全空却标 complete"的结果，
    # 反而覆盖掉好的产物。这里拦住，让它回去重跑数量把缓存重建出来。
    if not (CACHE_DIR / f'{name}.sqlite').exists():
        return {'basic': False, 'lengths': False, 'reason': '候选缓存 sqlite 缺失，需重跑数量'}
    length_ok = (length_result.get('status') == 'complete'
                 and length_result.get('fingerprint') == signature
                 and (length_result.get('samples_measured') or 0) > 0)
    reason = '' if length_ok else f"长度 {length_result.get('status') or '还没做'}"
    return {'basic': True, 'lengths': length_ok, 'reason': reason}


def build_plan(manifest: dict, datasets=None, config_ids=None, force=False) -> list[dict]:
    rows = []
    for name, entry, config in configurations(manifest, datasets, config_ids):
        state, detail = input_state(entry, config)
        done = completion(entry, config, force) if state != 'declared_only' else {'basic': False, 'lengths': False, 'reason': '登记声明'}
        if state == 'declared_only':
            action = '登记'      # 跑一次把声明写进结果，之后视为已完成
        elif state in ('missing', 'partial'):
            action = '阻塞'
        elif not done['basic']:
            action = '数量+长度'
        elif not done['lengths']:
            action = '长度'
        else:
            action = '跳过'
        rows.append({'dataset': name, 'display': entry.get('display_name', name),
                     'config': config['config_id'], 'subset': config.get('_subset') or '',
                     'stem': stem(config), 'papers': '; '.join(config.get('papers', [])),
                     'split': config.get('split'), 'state': state, 'detail': detail,
                     'action': action, **done})
    return rows


def summarize_plan(rows: list[dict]) -> list[dict]:
    grouped = OrderedDict()
    for row in rows:
        item = grouped.setdefault(row['dataset'], {'dataset': row['dataset'], 'total': 0,
                                                  '跳过': 0, '数量+长度': 0, '长度': 0,
                                                  '登记': 0, '阻塞': 0})
        item['total'] += 1
        item[row['action']] += 1
    return list(grouped.values())


def print_plan(rows: list[dict], present: list[str]) -> None:
    banner('计划')
    print('数据集目录里已有内容的：' + (', '.join(present) if present else '（无）'))
    print()
    header = f"{'数据集':<28}{'配置':>5}{'跳过':>6}{'待数量':>7}{'待长度':>7}{'登记':>6}{'阻塞':>6}"
    print(header)
    print('-' * len(header))
    for item in summarize_plan(rows):
        print(f"{item['dataset']:<28}{item['total']:>5}{item['跳过']:>6}"
              f"{item['数量+长度']:>7}{item['长度']:>7}{item['登记']:>6}{item['阻塞']:>6}")
    print('-' * len(header))
    totals = {key: sum(item[key] for item in summarize_plan(rows))
              for key in ('total', '跳过', '数量+长度', '长度', '登记', '阻塞')}
    print(f"{'合计':<28}{totals['total']:>5}{totals['跳过']:>6}"
          f"{totals['数量+长度']:>7}{totals['长度']:>7}{totals['登记']:>6}{totals['阻塞']:>6}")
    print()
    print('说明：跳过＝数量与长度都已完成且输入指纹未变；登记＝只记录论文声明（不读文件）；')
    print('      阻塞＝输入文件缺失或字节数不符（多为还没上传完），统计前必须先补齐。')

    todo = [row for row in rows if row['action'] in ('数量+长度', '长度', '登记')]
    blocked = [row for row in rows if row['action'] == '阻塞']
    if todo:
        print()
        print(f'本轮将处理 {len(todo)} 条（stem 里已经含子集名）：')
        for index, row in enumerate(todo, 1):
            # stem 是 ASCII，可以安全用宽度对齐；动作标签含中文不参与对齐
            print(f"  {index:>3}. {row['stem']:<42}[{row['action']}] {row['detail']}")
    if blocked:
        print()
        print(f'被阻塞的 {len(blocked)} 条（本轮跳过，原因逐条列出）：')
        for row in blocked:
            print(f"  - {row['stem']}: {row['detail']}")


def datasets_needing_extract(manifest: dict, rows: list[dict]) -> list[str]:
    """看起来需要先解压的数据集：该数据集有压缩包，且待跑配置需要的文件现在找不到。"""
    wanted = {row['dataset'] for row in rows if row['action'] in ('数量+长度', '长度')}
    hints = []
    for name in wanted:
        entry = manifest['datasets'][name]
        base = dataset_path(entry)
        archives = [p for p in real_files(base)
                    if p.name.endswith(('.tar.gz', '.tgz', '.zip', '.tar'))]
        if not archives:
            continue
        configs = [c for _, _, c in configurations(manifest, [name])]
        still_missing = any(input_state(entry, c)[0] == 'missing' for c in configs)
        if still_missing:
            hints.append(name)
    return hints


def prepare_inputs(manifest: dict, present: list[str], do_extract: bool) -> None:
    """刷新文件清单；需要时解压。都不因失败中断整轮。"""
    print()
    print('[1/4] 核对输入文件清单（check_data.py --all）', flush=True)
    result = subprocess.run([sys.executable, str(IR_CODE / 'scripts/check_data.py'), '--all'],
                            check=False)
    if result.returncode != 0:
        print('  ⚠ 文件清单显示有缺项；本轮会跳过相应配置，先继续把能做的做完。', flush=True)
    print(f'  → 明细已更新：{to_display(OUTPUTS_DIR / "inventory.csv")}', flush=True)

    if not do_extract:
        print()
        print('[解压] 已跳过（默认不解压：extract_data.py 核对通过后会删原包，体积可能很大）')
        return
    if not present:
        return
    print()
    print(f'[解压] 处理 {len(present)} 个数据集（无需解压的会自动跳过）', flush=True)
    subprocess.run([sys.executable, str(IR_CODE / 'scripts/extract_data.py'), '--dataset', *present],
                   check=False)


def write_status(state: str, rows: list[dict], results: list[dict]) -> None:
    write_json(OUTPUTS_DIR / 'run_status.json', {
        'updated': time.strftime('%Y-%m-%d %H:%M:%S'), 'status': state,
        '计划条数': len(rows),
        '本轮处理': [row['stem'] for row in rows if row['action'] in ('数量+长度', '长度', '登记')],
        '结果': results})


def process(manifest: dict, row: dict, args) -> dict:
    """跑一条配置。返回结果摘要；异常不外抛，交给调用方记录。"""
    name, entry = row['dataset'], manifest['datasets'][row['dataset']]
    config = None
    for _, _, candidate in configurations(manifest, [row['dataset']], [row['config']]):
        if stem(candidate) == row['stem']:
            config = candidate
            break
    if config is None:
        return {'stem': row['stem'], 'ok': False, 'error': 'config 已不在 manifest 里'}

    print()
    banner(f"{row['stem']}   [{row['action']}]")
    if row['papers']:
        print(f"论文：{row['papers']}     split={row['split']} 样本单位={config.get('sample_unit')}")
    started = time.time()
    record = {'stem': row['stem'], 'dataset': name, 'action': row['action'], 'ok': True,
              'basic': None, 'lengths': None, 'seconds': None, 'error': None}
    try:
        # 「登记」类和需要重跑数量的，都先过一次 basic（登记类不读文件，几乎不耗时）
        need_basic = row['action'] in ('数量+长度', '登记') or not row['basic']
        if need_basic and not args.lengths_only:
            result = basic(manifest, name, entry, config, force=args.force)
            record['basic'] = result.get('status')
            csv_update(OUTPUTS_DIR / 'experiment_stats.csv', [result])
            print(f"  数量：{result.get('status')}  query={result.get('query_count')}  "
                  f"候选池={result.get('pool_size')}{'*' if result.get('pool_status') == 'estimate' else ''}  "
                  f"平均正例={result.get('positives_per_query')}", flush=True)
        else:
            record['basic'] = 'skipped'
            print('  数量：已完成，跳过', flush=True)

        should_lengths = (row['action'] in ('数量+长度', '长度', '登记')
                          and (args.force or not row['lengths']))
        if should_lengths and not row['action'] == '登记':
            result = lengths(manifest, name, entry, config, limit=args.limit,
                             override=args.tokenizer, no_token=args.no_token)
            record['lengths'] = result.get('status')
            if not args.limit:
                csv_update(OUTPUTS_DIR / 'length_stats.csv', [result])
            print(f"  长度：{result.get('status')}  token均值={result.get('token_mean')}  "
                  f"word均值={result.get('word_mean')}  有效样本={result.get('samples_measured')}/"
                  f"{result.get('samples_dedup')}", flush=True)
            if result.get('status') not in ('complete', 'debug'):
                print(f"  ⚠ 未完成：{short(result.get('notes'), 120)}", flush=True)
        elif row['action'] != '登记':
            record['lengths'] = 'skipped'
            print('  长度：已完成，跳过', flush=True)
    except Exception as error:                      # noqa: BLE001 —— 单条失败不能中断整轮
        record['ok'] = False
        record['error'] = f'{type(error).__name__}: {error}'
        print(f"  ✗ 这条失败了：{record['error']}", flush=True)
        traceback.print_exc(limit=3)
    record['seconds'] = round(time.time() - started, 1)
    print(f"  用时 {record['seconds']}s", flush=True)
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description='一键续跑第二阶段统计；跳过已完成，继续未完成。')
    parser.add_argument('--dataset', nargs='*', help='只处理这些数据集')
    parser.add_argument('--config', nargs='*', help='只处理这些 config_id')
    parser.add_argument('--plan', action='store_true', help='只打印计划，不跑统计')
    parser.add_argument('--force', action='store_true', help='忽略已有结果，全部重跑')
    parser.add_argument('--lengths-only', action='store_true', help='只补长度，不重跑数量')
    parser.add_argument('--extract', action='store_true', help='先解压（默认不解压）')
    parser.add_argument('--limit', type=int, help='每条只统计前 N 个样本，结果写 debug 目录')
    parser.add_argument('--tokenizer', help='tokenizer 路径，默认取 manifest.globals')
    parser.add_argument('--no-token', action='store_true', help='只算 word，不加载 tokenizer')
    parser.add_argument('--no-merge', action='store_true', help='不每条都汇总，只在最后汇总一次')
    args = parser.parse_args()
    if args.limit is not None and args.limit <= 0:
        parser.error('--limit 必须大于零')

    manifest = load_manifest()
    banner('第二阶段统计 · 一键续跑')
    print(f"统计目录：{to_display(OUTPUTS_DIR.parent)}    输出：{to_display(OUTPUTS_DIR)}")
    print(f"tokenizer：{manifest['globals']['tokenizer']['path']}"
          f"{'（本轮 --no-token）' if args.no_token else ''}")
    if args.limit:
        print(f"⚠ --limit {args.limit}：只跑前 {args.limit} 个样本，结果写 "
              f"{to_display(OUTPUTS_DIR / 'debug')}，不覆盖正式汇总")

    # 已有内容的数据集：manifest 里 path 下确实有数据文件
    present = args.dataset or [name for name, entry in manifest['datasets'].items()
                               if any(p.suffix in ('.jsonl', '.json', '.parquet', '.tsv', '.txt', '.gz', '.zip', '.tar')
                                      for p in real_files(dataset_path(entry)))]

    rows = build_plan(manifest, args.dataset or None, args.config or None, args.force)
    rows.sort(key=lambda row: (ACTION_ORDER.index(row['action']) if row['action'] in ACTION_ORDER else 9,
                               dataset_rank(row['dataset']), row['stem']))
    write_status('planning', rows, [])
    print_plan(rows, present)

    hints = datasets_needing_extract(manifest, rows)
    if hints:
        print()
        print('提示：下面这些数据集有压缩包、且待跑配置现在找不到样本文本，可能需要先解压')
        print('      （要解压请加 --extract；注意 extract_data.py 核对通过后会删掉原包）：')
        print('      ' + ', '.join(hints))

    if args.plan:
        print()
        print('（--plan 模式，未执行统计）')
        return 0

    todo = [row for row in rows if row['action'] in ('数量+长度', '长度', '登记')]
    if not todo:
        print()
        print('没有需要处理的新配置：已完成的都跳过，其余要么被阻塞要么只登记声明。')
        print(f"细节见 {to_display(OUTPUTS_DIR / 'final_stats.json')} 与 "
              f"{to_display(OUTPUTS_DIR / 'issues.csv')}")
        return 0

    prepare_inputs(manifest, present, args.extract)

    print()
    print(f'[3/4] 开始处理 {len(todo)} 条配置', flush=True)
    results = []
    started = time.time()
    for index, row in enumerate(todo, 1):
        print()
        print('=' * 78)
        print(f'进度 [{index}/{len(todo)}]  已用 {int(time.time() - started)}s  '
              f'（还剩 {len(todo) - index + 1} 条）')
        print('=' * 78)
        record = process(manifest, row, args)
        results.append(record)
        write_status('running', rows, results)
        if not args.no_merge and not args.limit:
            subprocess.run([sys.executable, str(IR_CODE / 'scripts/merge_results.py'),
                            '--preview', '0'], check=False)

    print()
    print('[4/4] 汇总', flush=True)
    if not args.limit:
        subprocess.run([sys.executable, str(IR_CODE / 'scripts/merge_results.py'),
                        '--preview', '0'], check=False)
    write_status('finished', rows, results)

    ok = [r for r in results if r['ok']]
    bad = [r for r in results if not r['ok']]
    partial = [r for r in ok if r.get('lengths') not in (None, 'complete', 'debug', 'skipped')]
    banner('本轮结果')
    print(f"处理 {len(results)} 条：成功 {len(ok)} 条，失败 {len(bad)} 条，"
          f"其中长度未完成 {len(partial)} 条，用时 {int(time.time() - started)}s")
    if bad:
        print()
        print('失败清单：')
        for record in bad:
            print(f"  ✗ {record['stem']}：{record['error']}")
    if partial:
        print()
        print('长度未完成（多为正文没传完，补齐后重跑本脚本即可续上）：')
        for record in partial:
            print(f"  - {record['stem']}：{record['lengths']}")
    remaining = [row for row in rows if row['action'] == '阻塞']
    if remaining:
        print()
        print(f"另有 {len(remaining)} 条被阻塞（输入文件缺失或字节数不符），本轮未处理。")
    print()
    print(f"主汇总：{to_display(OUTPUTS_DIR / 'final_stats.json')}")
    print(f"逐条详情：{to_display(OUTPUTS_DIR / 'experiments')}    "
          f"问题项：{to_display(OUTPUTS_DIR / 'issues.csv')}    "
          f"进度：{to_display(OUTPUTS_DIR / 'run_status.json')}")
    print('随时可以 Ctrl-C；重跑本脚本会自动跳过已完成的配置。')
    return 1 if (bad or partial) else 0


if __name__ == '__main__':
    raise SystemExit(main())
