"""把子集统计合并到数据集/论文级；读取长度缓存，不重新编码。"""
import copy
from collections import Counter
import json
import math
import sqlite3
from pathlib import Path

from _common import OUTPUTS_DIR
from stage2 import write_json


def distribution(hist):
    total=sum(hist.values())
    if not total:return {'value':None,**{'P'+str(p):None for p in (25,50,75,90)}}
    out={'value':round(sum(v*n for v,n in hist.items())/total,2),'effective_samples':total}
    for pct in (25,50,75,90):
        target=math.ceil(pct/100*total);running=0
        for value,count in sorted(hist.items()):
            running+=count
            if running>=target:out['P'+str(pct)]=value;break
    return out


def aggregate(config,rows):
    if len(rows)==1:return copy.deepcopy(rows[0])
    result=copy.deepcopy(rows[0]);result['subset']=None
    result['合并范围']=[r.get('subset') or config['config_id'] for r in rows]
    result['合并规则']='query/doc ID 按各子集命名空间区分；正例对先求总数再除总 query；长度合并所有样本的频数，不平均子集均值或分位数。'
    basic_ready=all(r['数量状态']=='complete' for r in rows)
    length_ready=all(r['长度状态']=='complete' for r in rows)
    result['数量状态']='complete' if basic_ready else 'unavailable'
    result['长度状态']='complete' if length_ready else 'pending'
    if basic_ready:
        for field in ('query数量','候选池数量'):
            values=[r[field]['value'] for r in rows]
            if all(isinstance(v,(int,float)) for v in values):
                value=sum(values);result[field]['value']=value
                result[field]['display']=str(value)+('*' if result[field]['status']=='estimate' else '')
                result[field]['explanation']+='；合并 '+str(len(rows))+' 个子集，以各子集 ID 命名空间区分'
        positives=[r['每个query平均正样本数量'].get('positive_pairs') for r in rows]
        queries=result['query数量']['value']
        if queries and all(isinstance(v,(int,float)) for v in positives):
            metric=result['每个query平均正样本数量'];metric['positive_pairs']=sum(positives)
            metric['query_denominator']=queries;metric['value']=round(sum(positives)/queries,3);metric['display']=str(metric['value'])
        records=[r['query数量'].get('data_records') for r in rows]
        if all(isinstance(v,(int,float)) for v in records):result['query数量']['data_records']=sum(records)
    if length_ready:
        histograms={'token':Counter(),'word':Counter()}
        for row in rows:
            source=Path(row['原始结果文件']['长度']).name
            length=json.loads((OUTPUTS_DIR/'experiments'/source).read_text(encoding='utf-8'))
            cache=OUTPUTS_DIR/'cache'/(source.removesuffix('_lengths.json')+'.sqlite')
            db=sqlite3.connect(cache.as_uri()+'?mode=ro',uri=True)
            db.execute('PRAGMA cache_size=-2048')
            for prefix,column in [('token','tokens'),('word','words')]:
                histograms[prefix].update(dict(db.execute('SELECT '+column+',COUNT(*) FROM measured WHERE signature=? AND '+column+' IS NOT NULL GROUP BY '+column,(length['tokenizer_signature'],))))
            db.close()
        for prefix,field in [('token','每个样本平均token'),('word','每个样本平均word')]:
            metric=result[field];metric.update(distribution(histograms[prefix]))
            metric['display']=str(metric['value'])+('*' if metric.get('reference_only') else '')
            metric['explanation']+='；均值与四个分位数来自合并后的样本长度频数'
            metric.pop('occurrence_weighted',None)
        for field in ('去重样本数','已计量样本数','缺失或冲突正文'):
            result['长度覆盖'][field]=sum(r['长度覆盖'].get(field) or 0 for r in rows)
        result['长度覆盖']['覆盖率']=1.0
    else:
        result['未完成原因']='；'.join(dict.fromkeys(r.get('未完成原因') or '部分子集尚未完成' for r in rows if r['长度状态']!='complete'))
        for field in ('每个样本平均token','每个样本平均word'):
            result[field].update(value=None,display='该论文使用范围尚未全部完成长度统计',**{'P'+str(p):None for p in (25,50,75,90)})
    return result


def main():
    summary=json.loads((OUTPUTS_DIR/'final_stats.json').read_text(encoding='utf-8'))
    for dataset in summary['datasets'].values():
        for config in dataset['configs']:
            config['dataset_result']=aggregate(config,config['results'])
    write_json(OUTPUTS_DIR/'workbook_stats.json',summary)
    print('数据集/论文级合并结果：../ir_data/outputs/workbook_stats.json；未重新编码')


if __name__=='__main__':main()
