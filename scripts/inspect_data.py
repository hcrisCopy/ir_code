"""查看一条原始记录及同口径候选；只读取指定配置。"""
import argparse
import json
from _common import load_manifest,dataset_path,expand_files,short,load_tokenizer,count_tokens,count_words,banner
from stage2 import configurations,rows_of,documents,language

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--dataset');p.add_argument('--config',nargs='*');p.add_argument('--list',action='store_true');p.add_argument('--n',type=int,default=1);p.add_argument('--tokenizer');p.add_argument('--no-token',action='store_true');a=p.parse_args()
    m=load_manifest()
    if a.list:
        for name,e,c in configurations(m,[a.dataset] if a.dataset else None,a.config): print(name,c['config_id'],c.get('_subset',''))
        return 0
    if not a.dataset:p.error('指定 --dataset')
    for name,e,c in configurations(m,[a.dataset],a.config):
        banner(name+' / '+c['config_id']+' / '+str(c.get('_subset','')))
        print('配置',json.dumps(c,ensure_ascii=False))
        spec=c.get('sample_source') or {};files=expand_files(spec.get('file',''),dataset_path(e))
        if not files:print('没有可读取的样本文件');continue
        row=next(rows_of(files[0]));print('原始记录',short(row,1200))
        lang=language(e,row.get('source') or row.get('dataset')) if isinstance(row,dict) else language(e)
        kind=spec.get('kind')
        if kind in ('jsonl_list','jsonl_ids','parquet_nested','conversation'): ds=list(documents(row,c))[:a.n]
        elif kind in ('jsonl','parquet','question'):
            ts=c.get('text_source') or e.get('text_source') or {};fields=spec.get('text_fields') or ts.get('text_fields') or [spec.get('unit_field','text')]
            ds=[(row.get(spec.get('id_field',ts.get('id_field','id'))),'\n'.join(str(row[f]) for f in fields if row.get(f) is not None),None)]
        else:print('样本切分尚未实现');continue
        for did,text,qid in ds:
            print('候选 ID',did,'query ID',qid,'语言',lang,'正文',short(text,500))
            if text is None:print('正文需通过跨数据集 ID 关联；先运行数量/长度统计');continue
            words=count_words(text) if lang in ('zh','en') else None
            tokens=count_tokens(load_tokenizer(m['globals'],a.tokenizer),[text],False)[0] if not a.no_token else None
            print('token',tokens,'word',words)
    return 0
if __name__=='__main__':raise SystemExit(main())
