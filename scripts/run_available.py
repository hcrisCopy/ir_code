"""逐个处理已上传数据：检查、解压、数量、长度、汇总。支持断点重跑。"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from _common import IR_CODE, OUTPUTS_DIR, dataset_path, load_manifest, real_files
from stage2 import basic, configurations, csv_update, lengths, write_json


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
    write_json(OUTPUTS_DIR/'run_status.json',{'updated':time.strftime('%Y-%m-%d %H:%M:%S'),
                   'status':'finished','configs':summary})
    return 0

if __name__=='__main__': raise SystemExit(main())
