"""逐个处理已上传数据：检查、解压、数量、长度、汇总。支持断点重跑。"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from _common import IR_CODE, OUTPUTS_DIR, dataset_path, load_manifest, real_files
from stage2 import basic, configurations, csv_update, lengths, write_json


def report(summary):
    lines=['# 服务器第二阶段统计进度','',
           '统计目录与代码同级：`../ir_data/`；tokenizer：`../Qwen/Qwen3-1.7B`。',
           'Token 不截断、不含特殊 token，统计压缩前候选原文；word 仅中英文，中文每汉字计 1。',
           '', '| 配置 / 子集 | query | 候选池 | 平均正例 | 平均 token | 平均 word | 数量 / 长度状态 |',
           '|---|---:|---:|---:|---:|---:|---|']
    for row in summary:
        suffix=row['config']+('__'+row['subset'] if row.get('subset') else '')
        b=json.loads((OUTPUTS_DIR/'experiments'/(suffix+'.json')).read_text(encoding='utf-8'))
        path=OUTPUTS_DIR/'experiments'/(suffix+'_lengths.json')
        l=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        pool=str(b.get('pool_size')) if b.get('pool_size') is not None else '未知/不适用'
        if b.get('pool_status')=='estimate': pool+='*'
        values=[suffix,b.get('query_count'),pool,b.get('positives_per_query'),l.get('token_mean'),l.get('word_mean'),row['basic']+'/'+row['lengths']]
        lines.append('| '+' | '.join(str(v) if v is not None else '—' for v in values)+' |')
    lines += ['', '星号：全语料/大池子/无 doc ID 的正文并集替代值。公开全集参考不能冒充论文未公开子集。',
              '四个分位数、有效样本数、语言和覆盖率见 `final_stats.csv`；未就绪与标星原因见 `issues.csv`。',
              '上传中的文件不覆盖、不截断、不按前缀生成正式结果。']
    path=OUTPUTS_DIR/'reports/run_report.md';path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text('\n'.join(lines)+'\n',encoding='utf-8')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset',nargs='*')
    parser.add_argument('--tokenizer')
    parser.add_argument('--no-extract',action='store_true')
    args=parser.parse_args()
    manifest=load_manifest()
    present=args.dataset or [name for name,e in manifest['datasets'].items()
        if any(p.suffix in ('.jsonl','.parquet','.gz','.zip','.tar') for p in real_files(dataset_path(e)))]
    print('现有数据目录：', ', '.join(present),flush=True)
    subprocess.run([sys.executable,str(IR_CODE/'scripts/check_data.py'),'--all'],check=False)
    if present and not args.no_extract:
        subprocess.run([sys.executable,str(IR_CODE/'scripts/extract_data.py'),'--dataset',*present],check=True)
    # 先完成小型、完整输入，再编码较大的公开训练集。
    priority=['auxiliary_math','rearank_12k','r2med','fullrank_training_data','bright',
              'rank_zephyr_training_data','reasonrank_data_13k','e2rank_ranking_datasets','pe_rank_training_data','miriad_4_4m']
    present.sort(key=lambda n:priority.index(n) if n in priority else len(priority))
    summary=[]
    for name,entry,config in configurations(manifest,present):
        write_json(OUTPUTS_DIR/'run_status.json',{'updated':time.strftime('%Y-%m-%d %H:%M:%S'),
                   'status':'running','active_config':config['config_id'],'active_subset':config.get('_subset'),
                   'configs':summary})
        print('\n处理',name,config['config_id'],config.get('_subset',''),flush=True)
        b=basic(manifest,name,entry,config)
        csv_update(OUTPUTS_DIR/'experiment_stats.csv',[b])
        if b['status']=='complete':
            l=lengths(manifest,name,entry,config,override=args.tokenizer)
            csv_update(OUTPUTS_DIR/'length_stats.csv',[l])
            summary.append({'dataset':name,'config':config['config_id'],'subset':config.get('_subset'),
                            'basic':b['status'],'lengths':l['status'],'notes':l['notes']})
        else:
            summary.append({'dataset':name,'config':config['config_id'],'subset':config.get('_subset'),
                            'basic':b['status'],'lengths':'unavailable','notes':b['notes']})
        write_json(OUTPUTS_DIR/'run_status.json',{'updated':time.strftime('%Y-%m-%d %H:%M:%S'),
                   'status':'running','configs':summary})
        subprocess.run([sys.executable,str(IR_CODE/'scripts/merge_results.py'),'--preview','0'],check=False)
        report(summary)
    write_json(OUTPUTS_DIR/'run_status.json',{'updated':time.strftime('%Y-%m-%d %H:%M:%S'),
                   'status':'finished','configs':summary})
    return 0

if __name__=='__main__': raise SystemExit(main())
