"""第二阶段共用引擎。流式解析，SQLite 去重，结果保存在 ir_data。"""
from __future__ import annotations

import argparse
import copy
import csv
import gzip
import hashlib
import json
import math
import os
import re
import sqlite3
import time
from collections import Counter
from pathlib import Path

from _common import (CACHE_DIR, IR_CODE, OUTPUTS_DIR, banner, count_tokens,
                     count_words, dataset_path, expand_files, iter_jsonl,
                     iter_parquet, load_manifest, load_tokenizer, progress,
                     short, to_display)

ENGINE_VERSION = 2
PCTS = (25, 50, 75, 90)


def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, path)


def csv_update(path, rows, keys=('dataset', 'config_id', 'subsets')):
    old = []
    if path.exists():
        with path.open(encoding='utf-8-sig', newline='') as handle:
            old = list(csv.DictReader(handle))
    index = {tuple(str(r.get(k) or '') for k in keys): r for r in old}
    for row in rows:
        index[tuple(str(row.get(k) or '') for k in keys)] = row
    fields = list(dict.fromkeys(k for row in index.values() for k in row if k != 'subset_stats'))
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.csv.tmp')
    with temp.open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(index.values())
    os.replace(temp, path)


def configurations(manifest, datasets=None, config_ids=None):
    names = datasets or list(manifest['datasets'])
    unknown = set(names) - manifest['datasets'].keys()
    if unknown:
        raise ValueError('未知数据集：' + ','.join(sorted(unknown)))
    seen = set()
    for name in names:
        entry = manifest['datasets'][name]
        for config in entry.get('configs', []):
            if config_ids and config['config_id'] not in config_ids:
                continue
            seen.add(config['config_id'])
            for subset in config.get('subsets') or [None]:
                out = copy.deepcopy(config)
                out.pop('subsets', None)
                if subset:
                    out['_subset'] = subset
                    def replace(value):
                        if isinstance(value, dict): return {k: replace(v) for k, v in value.items()}
                        if isinstance(value, list): return [replace(v) for v in value]
                        if isinstance(value, str):
                            for key in ('<subset>', '<name>', '<domain>'):
                                value = value.replace(key, subset)
                        return value
                    out = replace(out)
                yield name, entry, out
    if config_ids and set(config_ids) - seen:
        raise ValueError('指定 config 未匹配：' + ','.join(sorted(set(config_ids) - seen)))


def stem(config):
    return config['config_id'] + ('__' + config['_subset'] if config.get('_subset') else '')


def paths_for(entry, config):
    base = dataset_path(entry)
    patterns = [(config.get('sample_source') or {}).get('file'),
                (config.get('query') or {}).get('file'),
                (config.get('qrels') or {}).get('file')]
    files = []
    for pattern in patterns:
        if pattern and pattern not in ('n/a', 'relevant_docids'):
            files.extend(expand_files(pattern.split('#')[0], base))
    return sorted(set(files))


_CONTENT_SIGNATURE_CACHE: dict[tuple[str, int, int], str] = {}


def content_signature(path: Path) -> str:
    """文件内容指纹：大小 + 首尾各 64 KiB 的 SHA256。

    刻意【不】用 mtime。统计流程是"服务器上算、结果拿回本地汇总"，而文件在两台机器上
    的修改时间几乎必然不同（XFTP 不保留 mtime；rsync -a 虽保留，但历史上传的文件不一致）。
    指纹一旦含 mtime，服务器算好的 experiments/*.json 在本地会全部被判过期
    （summarize_results.result_for 的 stale 判定会把 query/候选池/正例/长度整体清空）。

    代价：改了文件中间、且前后 64 KiB 与总大小都没变的情况查不出来。
    "上传中的文件"不靠这个兜底——basic() 里另有一道按 manifest 声明字节数的精确拦截。
    """
    stat = path.stat()
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    cached = _CONTENT_SIGNATURE_CACHE.get(key)
    if cached is not None:
        return cached
    block = 64 * 1024
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        digest.update(handle.read(block))
        if stat.st_size > block:
            handle.seek(max(0, stat.st_size - block))
            digest.update(handle.read(block))
    value = digest.hexdigest()
    _CONTENT_SIGNATURE_CACHE[key] = value
    return value


def fingerprint(entry, config):
    signatures = [(to_display(p), p.stat().st_size, content_signature(p)) for p in paths_for(entry, config)]
    statistical_config=copy.deepcopy(config)
    legacy=statistical_config.pop('statistics_metadata_before_ranking_audit',None)
    if legacy is not None:
        # N/M 的证据与展示修正不改变候选、query、正例或原文长度。
        # 保持既有缓存指纹，避免把服务器已完成的编码作废。
        statistical_config['n_to_k']=legacy
    return hashlib.sha256(json.dumps([ENGINE_VERSION, entry.get('language'), statistical_config, signatures],
                                    sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def connect(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript('''
        PRAGMA journal_mode=WAL;
        PRAGMA cache_size=-8192;
        PRAGMA temp_store=FILE;
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS queries (qid TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS docs (id TEXT PRIMARY KEY, text TEXT, lang TEXT,
            occurrences INTEGER DEFAULT 1, text_hash TEXT, conflict INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS pairs (qid TEXT, did TEXT, PRIMARY KEY(qid,did));
        CREATE TABLE IF NOT EXISTS positives (qid TEXT, did TEXT, PRIMARY KEY(qid,did));
        CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS measured (id TEXT, signature TEXT, tokens INTEGER,
            words INTEGER, lang TEXT, PRIMARY KEY(id,signature));
    ''')
    return db


def language(entry, hint=None):
    """显式来源标签优先；未知语言不假装英文。"""
    value = str(hint or '').lower().replace('-', '_')
    if value.startswith('miracl_'):
        return value.split('_')[-1]
    if value in ('zh','en'):
        return value
    if value in ('cmedqav2','dureader','t2ranking','mmarco_chinese'):
        return 'zh'
    if value in ('msmarco','nq','hotpotqa','trivia','triviaqa','allnli','fever','eli5','squad','quora'):
        return 'en'
    langs = entry.get('language') or []
    return langs[0] if len(langs) == 1 else 'unknown'


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def rows_of(path):
    if path.name.endswith('.parquet'):
        yield from iter_parquet(path)
    elif path.name.endswith(('.jsonl', '.jsonl.gz')):
        yield from iter_jsonl(path)
    elif path.suffix == '.json':
        import ijson
        with path.open('rb') as handle:
            for key, value in ijson.kvitems(handle, ''):
                yield {'id': key, 'text': value}
    elif path.name.endswith(('.tsv','.txt','.tsv.gz','.txt.gz')):
        from _common import iter_tsv
        yield from iter_tsv(path)
    else:
        raise ValueError(f'未实现的文件格式：{to_display(path)}')


QUERY_PATTERN = re.compile(r'relevance to the search query:\s*(.*?)\n', re.S | re.I)
PASSAGE_PATTERN = re.compile(r'^\[(\d+)\]\s+(.*?)(?=^\[\d+\]\s+|\Z)', re.S | re.M)


def query_text(row):
    for key in ('query','question','query_text'):
        if isinstance(row.get(key), str):
            return row[key]
    for key in ('messages_w_content','messages','conversations'):
        for message in row.get(key) or []:
            if message.get('role') == 'user' or message.get('from') == 'human':
                prompt = message.get('content') or message.get('value') or ''
                match = QUERY_PATTERN.search(prompt + '\n')
                if match:
                    return match[1].strip().removesuffix('.')
    return None


def documents(row, config):
    spec = config.get('sample_source') or {}
    kind = spec.get('kind')
    if kind == 'parquet_nested':
        for hit in row.get(spec.get('unit_field','hits')) or []:
            yield str(hit['docid']), hit.get('content'), hit.get('qid')
    elif kind in ('jsonl_list','jsonl_ids'):
        field = spec.get('unit_field') or spec.get('id_field')
        values = row.get(field) or []
        if not isinstance(values, list): raise ValueError(f'{field} 应是列表')
        if spec.get('top_n'): values = values[:spec['top_n']]
        for value in values:
            if kind == 'jsonl_ids':
                yield str(value), None, None
            elif isinstance(value, str):
                yield None, value, None
            elif isinstance(value, dict):
                yield value.get('docid') or value.get('id'), value.get('content') or value.get('text'), None
            else:
                raise ValueError('候选不是文本或文档对象')
    elif kind == 'conversation':
        prompt = next((m.get('value') or m.get('content') for m in row.get('conversations', [])
                       if m.get('from') == 'human' or m.get('role') == 'user'), '')
        match = PASSAGE_PATTERN.findall(prompt)
        expected = spec.get('per_query')
        if expected and [int(i) for i, _ in match] != list(range(1,expected+1)):
            raise ValueError('对话候选编号不连续，不能自动切分')
        if match:
            last = match[-1][1]
            # RankZephyr 官方模板以这句指令终止最后一条候选。
            marker = '\nSearch Query:'
            if marker in last: last = last.split(marker)[0]
            for index, (_, text) in enumerate(match):
                yield None, last if index == len(match)-1 else text, None
        else:
            raise ValueError('对话模板未匹配到候选')


def qid_for(row, entry, path, line, config):
    namespace = str(row.get('dataset') or row.get('source') or config.get('_subset') or '')
    if isinstance(row, dict):
        for key in ('qid','query_id'):
            if row.get(key) is not None: return namespace + ':' + str(row[key]), 'id'
        if config.get('query',{}).get('id_field') not in ('hits',None):
            value = row.get(config['query']['id_field'])
            if value is not None: return namespace + ':' + str(value), 'id'
        text = query_text(row)
        if text: return namespace + ':text:' + digest(text), 'text'
        if row.get('id') is not None: return namespace + ':record:' + str(row['id']), 'record'
    return to_display(path) + ':record:' + str(line), 'record'


def add_doc(db, did, text, lang, namespace=''):
    hashed = digest(text) if text is not None else None
    identity = namespace + ':' + str(did) if did is not None else 'text:' + lang + ':' + hashed
    db.execute('''INSERT INTO docs(id,text,lang,text_hash) VALUES(?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET occurrences=occurrences+1,
        conflict=MAX(conflict,CASE WHEN docs.text_hash IS NOT NULL AND excluded.text_hash IS NOT NULL
            AND docs.text_hash != excluded.text_hash THEN 1 ELSE 0 END)''', (identity,text,lang,hashed))
    return identity


def read_queries(db, base, entry, config):
    spec = config.get('query') or {}
    target = spec.get('file')
    if not target: return 0
    for path in expand_files(target, base):
        for line, row in enumerate(rows_of(path)):
            if isinstance(row, list):
                if not row or row[0].lower() in ('query-id','qid','query_id'): continue
                qid = str(row[spec.get('index_field',0)])
            else:
                value = row.get(spec.get('id_field', 'id'))
                if value is None: raise ValueError('query 缺少配置指定的身份字段')
                qid = ':' + str(value)
            db.execute('INSERT OR IGNORE INTO queries VALUES(?)', (qid,))
            if line % 10000 == 0: db.commit()
    db.commit()
    if spec.get('selection_qrels'):
        selected=set()
        for path in expand_files(spec['selection_qrels'],base):
            for row in rows_of(path):
                if row and row[0] not in ('query-id','qid'): selected.add(':'+row[0])
        if not selected: raise ValueError('缺 split query 选择文件：'+spec['selection_qrels'])
        for (qid,) in db.execute('SELECT qid FROM queries').fetchall():
            if qid not in selected: db.execute('DELETE FROM queries WHERE qid=?',(qid,))
        db.commit()
    return db.execute('SELECT COUNT(*) FROM queries').fetchone()[0]


def read_positives(db, base, config):
    spec = config.get('qrels') or {}
    target = spec.get('file')
    if not target or target in ('n/a','relevant_docids'): return False
    filepart, _, nested = target.partition('#')
    files = expand_files(filepart, base)
    if not files: return False
    fields = spec.get('fields') or {'qid':0, 'docid':2, 'rel':3}
    threshold = spec.get('positive_threshold', '>0')
    match = re.fullmatch(r'(>=|>)\s*([0-9.]+)', str(threshold))
    if not match: raise ValueError('不支持的正例阈值：' + str(threshold))
    minimum = float(match[2])
    valid = 0
    for path in files:
        for index, row in enumerate(rows_of(path)):
            if nested:
                qid = str(row.get('id'))
                pairs = [(qid, str(d), 1) for d in row.get(nested) or []]
            elif isinstance(row, dict):
                try:
                    pairs = [(str(row[fields['qid']]), str(row[fields['docid']]), float(row[fields['rel']]))]
                except (KeyError,ValueError,TypeError) as error:
                    raise ValueError(f'qrels 字段/取值解析失败：{path.name} 第 {index+1} 条：{error}') from None
            else:
                if spec.get('header') and index == 0: continue
                if len(row) <= max(fields.values()):
                    raise ValueError(f'qrels 列数与配置不一致：{path.name} 第 {index+1} 行只有 {len(row)} 列，'
                                     f'配置要求到第 {max(fields.values())+1} 列')
                try:
                    pairs = [(row[fields['qid']],row[fields['docid']],float(row[fields['rel']]))]
                except (ValueError,TypeError) as error:
                    raise ValueError(f'qrels rel 列解析失败：{path.name} 第 {index+1} 行：{error}') from None
            for qid, did, rel in pairs:
                # corpus/query 文件均使用 :qid 命名；多来源训练使用显式关联。
                qid = ':' + qid
                if not db.execute('SELECT 1 FROM queries WHERE qid=?', (qid,)).fetchone(): continue
                valid += 1
                if rel > minimum or (match[1] == '>=' and rel == minimum):
                    db.execute('INSERT OR IGNORE INTO positives VALUES(?,?)',(qid,did))
            if index % 10000 == 0: db.commit()
    db.commit()
    if not valid: raise ValueError('qrels 没有匹配到当前 query 集，检查字段和身份')
    return True


def basic(manifest, name, entry, config, force=False):
    base = dataset_path(entry)
    output = OUTPUTS_DIR/'experiments'/f'{stem(config)}.json'
    sig = fingerprint(entry, config)
    if not force and output.exists():
        old = json.loads(output.read_text(encoding='utf-8'))
        if old.get('fingerprint') == sig and old.get('status') == 'complete':
            print('  [缓存] 数量结果未变')
            return old
    dbpath = CACHE_DIR/f'{stem(config)}.sqlite'
    for suffix in ('','-wal','-shm'):
        Path(str(dbpath)+suffix).unlink(missing_ok=True)
    db = connect(dbpath)
    spec = config.get('sample_source') or {}
    kind = spec.get('kind')
    result = dict(dataset=name,config_id=config['config_id'],papers='; '.join(config.get('papers',[])),
        split=config.get('split'),sample_unit=config.get('sample_unit'),subsets=config.get('_subset') or '',
        record_type=config.get('record_type') or ('训练集' if config.get('split')=='train' else '测试集'),
        record_name=config.get('record_name') or entry.get('display_name',name),
        query_count=None,query_count_status='unknown',paper_reported_query_count=(config.get('query') or {}).get('count'),
        training_record_count=0,n_to_k=(config.get('n_to_k') or {}).get('text'),
        pool_size=None,pool_status='unknown',pool_scope=config.get('pool_scope','candidate_union'),
        positives_total=None,positives_per_query=None,positives_status='unknown',
        positives_scope=config.get('positives_scope','annotated_pool'),status='unavailable',
        fingerprint=sig,notes=config.get('note') or '')
    notes = [result['notes']] if result['notes'] else []
    try:
        if kind in ('derived','external','mteb_task','tsv_triples','tsv_tuples',None) or config.get('usage_scope')=='unknown_subset':
            result['query_count'] = (config.get('query') or {}).get('count')
            result['query_count_status'] = (config.get('query') or {}).get('count_status') or 'unknown'
            if kind == 'mteb_task':
                result['query_count'] = None
                result['task_count'] = config.get('task_count')
            candidate = config.get('candidate') or {}
            result['pool_size'] = candidate.get('pool')
            result['pool_status'] = 'estimate' if candidate.get('pool') else ('na' if candidate.get('pool_status')=='na' else 'unknown')
            notes.append(spec.get('note') or '实际候选或子集未公开，不能以完整公开文件代替')
            qspec = config.get('query') or {}
            if qspec.get('count_status')=='na': result['query_count_status']='na'
            if (config.get('qrels') or {}).get('status')=='na': result['positives_status']='na'
            return result
        files = expand_files(spec.get('file',''), base)
        if not files: raise FileNotFoundError(spec.get('file'))
        # 已知文件大小不符时禁止对上传中的前缀计算正式统计。
        for declaration in entry.get('files',[]):
            if 'bytes' in declaration:
                for p in expand_files(declaration['path'],base):
                    if p in files and p.stat().st_size != declaration['bytes']:
                        raise ValueError(f'{p.name} 大小 {p.stat().st_size} / {declaration["bytes"]}，尚未完整')
        corpus = kind in ('jsonl','jsonl_gz','parquet','tsv','tsv_gz','question')
        expected_count = spec.get('min_files', 1)
        if len(files) < expected_count: raise ValueError(f'只有 {len(files)} / {expected_count} 个输入分片')
        identity_counts = Counter()
        has_positive_labels = False
        first = None
        for path in files:
            for line, row in enumerate(progress(rows_of(path), desc=path.name, unit='record')):
                result['training_record_count'] += 1
                if not corpus:
                    qid, idkind = qid_for(row,entry,path,line,config)
                    identity_counts[idkind] += 1
                    namespace = str(row.get('dataset') or row.get('source') or config.get('_subset') or '')
                    batch = list(documents(row, config))
                    if spec.get('per_query') and len(batch) != spec['per_query']:
                        raise ValueError(f'候选数量 {len(batch)} / {spec["per_query"]}，记录不符合配置')
                    if kind == 'parquet_nested' and batch:
                        qid = ':' + str(batch[0][2])
                    no_query = (config.get('query') or {}).get('count_status') == 'na'
                    if not no_query: db.execute('INSERT OR IGNORE INTO queries VALUES(?)',(qid,))
                    lang = language(entry, row.get('language') or row.get('source') or row.get('dataset'))
                    ids = []
                    for did,text,_ in batch:
                        identity = add_doc(db,did,text,lang,namespace)
                        ids.append(identity)
                        if not no_query: db.execute('INSERT OR IGNORE INTO pairs VALUES(?,?)',(qid,identity))
                    if config.get('positive_field'):
                        positives = row.get(config['positive_field'])
                        if positives is None: raise ValueError('缺少指定正例字段')
                        for did in positives:
                            db.execute('INSERT OR IGNORE INTO positives VALUES(?,?)',(qid,namespace+':'+str(did)))
                        has_positive_labels = True
                    elif config.get('positive_index_field'):
                        index = row.get(config['positive_index_field'])
                        if index is None or not 1 <= int(index) <= len(ids): raise ValueError('正例索引越界或缺失')
                        db.execute('INSERT OR IGNORE INTO positives VALUES(?,?)',(qid,ids[int(index)-1]))
                        has_positive_labels = True
                else:
                    textspec = config.get('text_source') or entry.get('text_source') or {}
                    idfield = spec.get('id_field',textspec.get('id_field','id'))
                    fields = spec.get('text_fields') or textspec.get('text_fields') or [spec.get('unit_field','text')]
                    if isinstance(row, dict):
                        text='\n'.join(str(row[f]) for f in fields if row.get(f) is not None)
                        did='|'.join(str(row[f]) for f in spec['id_fields']) if spec.get('id_fields') else row.get(idfield)
                    else:
                        text='\n'.join(row[f] for f in fields); did=row[idfield]
                    lang=language(entry)
                    add_doc(db,did,text,lang)
                    qid=None
                    if kind=='question':
                        db.execute('INSERT OR IGNORE INTO queries VALUES(?)',(':'+str(did),))
                if first is None:
                    first = {'query_id':qid,'candidate':short(text if corpus else (batch[0][1] if batch else None),140),'language':lang}
                    print('  [样本]',json.dumps(first,ensure_ascii=False),flush=True)
                if line % 2000 == 0: db.commit()
        db.commit()
        if fingerprint(entry,config) != sig: raise ValueError('输入文件在读取期间发生变化，请等待上传完成后重跑')
        if corpus and kind != 'question':
            read_queries(db,base,entry,config)
            if (config.get('query') or {}).get('file') and not db.execute('SELECT 1 FROM queries LIMIT 1').fetchone():
                raise FileNotFoundError('缺当前 split 的 query 文件：'+str(config['query']['file']))
        nq=db.execute('SELECT COUNT(*) FROM queries').fetchone()[0]
        nd=db.execute('SELECT COUNT(*) FROM docs').fetchone()[0]
        content_only=db.execute("SELECT COUNT(*) FROM docs WHERE id LIKE 'text:%'").fetchone()[0]
        conflicts=db.execute('SELECT COUNT(*) FROM docs WHERE conflict!=0').fetchone()[0]
        result.update(query_count=nq or None,query_count_status='measured' if nq else 'na',pool_size=nd,
                      pool_status='estimate' if corpus or content_only else 'measured',status='complete',
                      query_identity=json.dumps(identity_counts),candidate_text_conflicts=conflicts,
                      length_identity='text_hash' if content_only else 'doc_id',unique_query_doc_pairs=db.execute('SELECT COUNT(*) FROM pairs').fetchone()[0])
        if corpus: result['pool_scope']='full_corpus'
        if config.get('pool_scope')=='na': result.update(pool_size=None,pool_status='na',pool_scope='na')
        if kind=='question': result.update(pool_size=None,pool_status='na',pool_scope='na',positives_status='na')
        if content_only: notes.append('无原始 doc ID；正文 SHA256 去重用于长度，正文并集数量为替代值，标 *')
        if identity_counts.get('text'): notes.append('query 按提取的 query 文本 SHA256 去重，不以增强记录数代替')
        if identity_counts.get('record'): notes.append('无法提取原始 query，按记录身份统计，query 数为代理值')
        if identity_counts.get('record') and nq: result['query_count_status']='estimate'
        reported=result['paper_reported_query_count']
        if reported and reported!=nq: notes.append(f'论文/历史表报告 {reported}；本公开文件实际 query {nq}，实测不采用论文数量作分母')
        if result['pool_scope']=='full_corpus': notes.append('无实际候选 run；池规模及长度为全语料替代统计，候选池标 *')
        if config.get('usage_scope')=='public_release': notes.append('公开全集参考统计，不能冒充论文未公开的抽样/阶段划分')
        qrels_error=None
        try:
            qrels_ok=has_positive_labels or read_positives(db,base,config)
        except (ValueError,FileNotFoundError,KeyError,json.JSONDecodeError,EOFError,OSError) as error:
            qrels_ok,qrels_error=False,error
        if qrels_ok:
            total=db.execute('SELECT COUNT(*) FROM positives').fetchone()[0]
            result.update(positives_total=total,positives_per_query=round(total/nq,3) if nq else None,positives_status='measured')
        elif qrels_error is not None:
            # qrels 是辅助文件：读挂只影响正例指标，不陪葬已实测的 query/候选池。
            # qrels 文件参与指纹（paths_for），其内容/就位状态变化会触发整条重算。
            result['positives_status']='unknown'
            notes.append(f'正例统计失败（query 数与候选池仍为实测值）：{qrels_error}')
        elif (config.get('qrels') or {}).get('status')=='na': result['positives_status']='na'
        elif ((config.get('qrels') or {}).get('file') or '') not in ('','n/a','relevant_docids'):
            notes.append('qrels 文件缺失或未解压，正例未统计；文件就位后指纹变化会自动重算')
        if conflicts: notes.append(f'{conflicts} 个 ID 对应不同正文，长度会跳过冲突 ID')
        db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('fingerprint',sig)); db.commit()
        result['sample_preview']=first
    except (ValueError,FileNotFoundError,KeyError,json.JSONDecodeError,EOFError,OSError) as error:
        result.update(status='unavailable',query_count=None,query_count_status='unknown',pool_size=None,pool_status='unknown')
        notes.append(f'输入未就绪或解析失败：{error}')
    finally:
        result['notes']='；'.join(notes)
        db.close()
        write_json(output,result)
    return result


def histogram_summary(db, signature, column, weighted=False):
    weight='d.occurrences' if weighted else '1'
    rows=db.execute(f'''SELECT m.{column},SUM({weight}) FROM measured m JOIN docs d ON d.id=m.id
        WHERE m.signature=? AND m.{column} IS NOT NULL GROUP BY m.{column} ORDER BY m.{column}''',(signature,)).fetchall()
    total=sum(n for _,n in rows)
    answer={'count':total,'mean':round(sum(v*n for v,n in rows)/total,2) if total else None}
    for p in PCTS:
        rank=math.ceil(p*total/100); running=0; answer[f'p{p}']=None
        for value,n in rows:
            running+=n
            if total and running>=rank: answer[f'p{p}']=value; break
    return answer


def tokenizer_signature(manifest, override=None):
    raw=override or manifest['globals']['tokenizer']['path']
    path=(IR_CODE/raw).resolve()
    files=[]
    for name in ('tokenizer.json','tokenizer_config.json','vocab.json','merges.txt','special_tokens_map.json'):
        p=path/name
        if p.exists():
            with p.open('rb') as handle:
                h=hashlib.file_digest(handle,'sha256').hexdigest()
            files.append((name,h))
    if not files: raise FileNotFoundError('tokenizer 文件不存在：'+to_display(path))
    return hashlib.sha256(json.dumps([files,manifest['globals']['word_rule'],ENGINE_VERSION]).encode()).hexdigest(),to_display(path)


def resolve_external(db, manifest, name, entry, config):
    """流式读取大型 ID 映射；不把数 GB JSON 装入内存。"""
    spec=config.get('sample_source') or {}
    textname=spec.get('text_dataset') or config.get('text_dataset') or name
    other=manifest['datasets'][textname]
    source=config.get('text_source') or other.get('text_source') or {}
    base=dataset_path(other)
    files=expand_files(source.get('file',''),base)
    missing=db.execute('SELECT COUNT(*) FROM docs WHERE text IS NULL').fetchone()[0]
    if not missing: return []
    if not files: return ['缺正文文件：'+str(source.get('file'))]
    errors=[]
    for path in files:
        namespace=path.stem if source.get('kind')=='jsonl_plus_idmap' else ''
        wanted = {row[0] for row in db.execute('SELECT id FROM docs WHERE text IS NULL AND id LIKE ?', (namespace+':%',))}
        if not wanted: continue
        initial_size = path.stat().st_size
        expected_size = next((d['bytes'] for d in other.get('files',[]) if 'bytes' in d and path in expand_files(d['path'],base)),None)
        if expected_size is not None and initial_size != expected_size:
            errors.append(f'{path.name} 正文仍未完整：{initial_size} / {expected_size} 字节')
            continue
        bar=progress(rows_of(path),desc='关联正文 '+path.name,unit='doc')
        try:
            for index,row in enumerate(bar):
                if isinstance(row,list):
                    did=row[source.get('id_field',0)]
                    text='\n'.join(row[f] for f in source.get('text_fields',[1]))
                else:
                    did=row.get('id') if source.get('kind')=='jsonl_plus_idmap' else row.get(source.get('id_field','id'))
                    text=row.get('text') if source.get('kind')=='jsonl_plus_idmap' else '\n'.join(str(row[f]) for f in source.get('text_fields',['text']) if row.get(f) is not None)
                identity=namespace+':'+str(did)
                if identity in wanted:
                    db.execute('UPDATE docs SET text=?,text_hash=? WHERE id=? AND text IS NULL',(text,digest(text),identity))
                if index%10000==0: db.commit()
        except Exception as error:
            errors.append(f'{path.name} 正文未完整：{error}')
        if path.stat().st_size != initial_size:
            errors.append(f'{path.name} 正文仍在上传，本次为部分结果')
        db.commit()
    return errors


def lengths(manifest,name,entry,config,limit=None,override=None,no_token=False):
    basicpath=OUTPUTS_DIR/'experiments'/f'{stem(config)}.json'
    output=OUTPUTS_DIR/'experiments'/f'{stem(config)}_lengths.json'
    result=dict(dataset=name,config_id=config['config_id'],subsets=config.get('_subset') or '',sample_unit=config.get('sample_unit'),
        status='unavailable',token_mean=None,word_mean=None,notes='')
    for prefix in ('token','word'):
        for p in PCTS: result[f'{prefix}_p{p}']=None
    if not basicpath.exists():
        result['notes']='先运行 count_basic.py'; write_json(output,result); return result
    b=json.loads(basicpath.read_text(encoding='utf-8'))
    if b.get('status')!='complete' or b.get('fingerprint')!=fingerprint(entry,config):
        result['notes']='实际候选/完整输入未就绪或配置已变化，请先重跑数量统计'; write_json(output,result); return result
    db=connect(CACHE_DIR/f'{stem(config)}.sqlite')
    notes=resolve_external(db,manifest,name,entry,config)
    sig,tokenpath=tokenizer_signature(manifest,override) if not no_token else ('word-only-v2','未运行')
    tokenizer=load_tokenizer(manifest['globals'],override) if not no_token else None
    batch_size=manifest['globals']['tokenizer'].get('batch_size',64)
    total=db.execute('SELECT COUNT(*) FROM docs').fetchone()[0]
    target=db.execute('''SELECT COUNT(*) FROM docs d WHERE text IS NOT NULL AND conflict=0
        AND NOT EXISTS(SELECT 1 FROM measured m WHERE m.id=d.id AND m.signature=?)''',(sig,)).fetchone()[0]
    query='''SELECT d.id,d.text,d.lang,d.text_hash FROM docs d WHERE d.text IS NOT NULL AND d.conflict=0
        AND NOT EXISTS(SELECT 1 FROM measured m WHERE m.id=d.id AND m.signature=?)'''
    cursor=db.execute(query,(sig,))
    shared=sqlite3.connect(CACHE_DIR/'text_lengths.sqlite')
    shared.execute('PRAGMA cache_size=-4096')
    shared.execute('CREATE TABLE IF NOT EXISTS lengths(hash TEXT, signature TEXT, tokens INTEGER, words INTEGER, PRIMARY KEY(hash,signature))')
    bar=progress(range(min(target,limit) if limit else target),total=min(target,limit) if limit else target,desc='编码唯一候选',unit='doc')
    done=0
    reused=0
    try:
        while True:
            batch=cursor.fetchmany(min(batch_size,limit-done) if limit else batch_size)
            if not batch: break
            cached=[shared.execute('SELECT tokens,words FROM lengths WHERE hash=? AND signature=?',(r[3],sig)).fetchone() for r in batch]
            fresh=[i for i,item in enumerate(cached) if item is None]
            texts=[batch[i][1] for i in fresh]
            tokens=count_tokens(tokenizer,texts,False) if tokenizer and texts else [None]*len(texts)
            for i,nt in zip(fresh,tokens):
                nw=count_words(batch[i][1]); cached[i]=(nt,nw)
                shared.execute('INSERT OR REPLACE INTO lengths VALUES(?,?,?,?)',(batch[i][3],sig,nt,nw))
            shared.commit()
            reused+=len(batch)-len(fresh)
            for (did,text,lang,_),(nt,raw_words) in zip(batch,cached):
                nw=raw_words if lang in ('zh','en') else None
                db.execute('INSERT OR REPLACE INTO measured VALUES(?,?,?,?,?)',(did,sig,nt,nw,lang))
            db.commit(); done+=len(batch)
            if hasattr(bar,'update'): bar.update(len(batch))
            if limit and done>=limit: break
    finally:
        if hasattr(bar,'close'): bar.close()
        shared.close()
    for prefix,column in (('token','tokens'),('word','words')):
        s=histogram_summary(db,sig,column)
        result[prefix+'_samples']=s['count']
        for field in ('mean','p25','p50','p75','p90'): result[prefix+'_'+field]=s[field]
        m=histogram_summary(db,sig,column,True)
        result[prefix+'_mean_multi']=m['mean']
        for p in PCTS: result[f'{prefix}_p{p}_multi']=m[f'p{p}']
    found=db.execute('SELECT COUNT(*) FROM measured WHERE signature=?',(sig,)).fetchone()[0]
    langs=dict(db.execute('SELECT lang,COUNT(*) FROM measured WHERE signature=? GROUP BY lang',(sig,)))
    missing=db.execute('SELECT COUNT(*) FROM docs WHERE text IS NULL OR conflict!=0').fetchone()[0]
    result.update(status='debug' if limit else ('partial' if missing or notes else 'complete'),
        fingerprint=b['fingerprint'],tokenizer_path=tokenpath,tokenizer_signature=sig,samples_dedup=total,
        samples_measured=found,missing_text=missing,coverage=round(found/total,6) if total else 0,
        language=json.dumps(langs,ensure_ascii=False),excluded_other_language=sum(n for k,n in langs.items() if k not in ('zh','en')),
        length_scope=b['pool_scope'],length_identity=b.get('length_identity'),
        samples_total=db.execute('SELECT SUM(occurrences) FROM docs').fetchone()[0])
    notes.append('Qwen3-1.7B；无特殊 token、不截断、压缩前原文；word 中文每汉字 1、英文单词、数字串 1、标点不计')
    notes.append('主分布按 doc ID 或明确标记的正文哈希去重；多次分布按候选出现次数加权')
    notes.append(f'复用 {reused} 条跨配置正文长度缓存；本配置新计量 {done-reused} 条')
    if langs.get('unknown'): notes.append('未知语言未计 word，不能冒充中英文')
    if missing: notes.append(f'{missing} 个候选正文缺失/冲突，部分结果不能作完整统计')
    result['notes']='；'.join(notes); db.close()
    write_json(output if not limit else OUTPUTS_DIR/'debug'/output.name,result)
    return result


def cli(phase):
    parser=argparse.ArgumentParser(description='第二阶段 '+phase)
    parser.add_argument('--dataset',nargs='*'); parser.add_argument('--config',nargs='*')
    parser.add_argument('--all',action='store_true'); parser.add_argument('--list',action='store_true')
    parser.add_argument('--force',action='store_true'); parser.add_argument('--limit',type=int)
    parser.add_argument('--tokenizer'); parser.add_argument('--no-token',action='store_true')
    args=parser.parse_args()
    if args.limit is not None and args.limit<=0: parser.error('--limit 必须大于零')
    if args.all and args.dataset: parser.error('--all 与 --dataset 不能同时使用')
    manifest=load_manifest()
    if args.list:
        for name,entry,config in configurations(manifest,args.dataset,args.config):
            print(name,stem(config),config.get('split'),config.get('usage_scope',''))
        return 0
    if not args.all and not args.dataset: parser.error('请指定 --dataset 或 --all')
    rows=[]; failed=False
    for name,entry,config in configurations(manifest,None if args.all else args.dataset,args.config):
        banner(name+' / '+stem(config))
        row=basic(manifest,name,entry,config,args.force) if phase=='数量' else lengths(manifest,name,entry,config,args.limit,args.tokenizer,args.no_token)
        rows.append(row)
        print(json.dumps({k:row.get(k) for k in ('status','query_count','pool_size','pool_status','positives_per_query','token_mean','word_mean','notes')},ensure_ascii=False),flush=True)
        failed=failed or row['status']=='partial'
    if rows and not args.limit:
        csv_update(OUTPUTS_DIR/('experiment_stats.csv' if phase=='数量' else 'length_stats.csv'),rows)
    return 1 if failed else 0
