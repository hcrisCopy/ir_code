"""按 config 与子集合并全部指标；保留状态，生成逐表行回填结果。"""
import argparse
import csv
import json
from _common import OUTPUTS_DIR, load_manifest, banner
from stage2 import csv_update, configurations
from summarize_results import write_summary, ranking_setting

STATUS = {'unknown':'未说明','na':'不适用','estimate':'*'}
def marked(value,status):
    if value is None or value == '': return STATUS.get(status,'未统计')
    return str(value)+('*' if status=='estimate' else '')

def build(manifest):
    final=[]; issues=[]
    for name,entry,config in configurations(manifest):
        suffix= config['config_id']+('__'+config['_subset'] if config.get('_subset') else '')
        basepath=OUTPUTS_DIR/'experiments'/(suffix+'.json')
        lengthpath=OUTPUTS_DIR/'experiments'/(suffix+'_lengths.json')
        if not basepath.exists(): continue
        b=json.loads(basepath.read_text(encoding='utf-8'))
        l=json.loads(lengthpath.read_text(encoding='utf-8')) if lengthpath.exists() else {}
        if l.get('fingerprint')!=b.get('fingerprint'): l={}
        row={'记录类型':b['record_type'],'数据集名称':b['record_name'],'config':b['config_id'],
             '子集':b.get('subsets',''),'论文':b['papers'],'split':b['split'],'样本单位':b['sample_unit'],
             'query数量':marked(b.get('query_count'),b.get('query_count_status')),
             '论文报告query数量':b.get('paper_reported_query_count'),'训练或数据记录数':b.get('training_record_count'),
             '样本多少出多少':ranking_setting(name,config)['display'],'候选池数量':marked(b.get('pool_size'),b.get('pool_status')),
             '候选池口径':b.get('pool_scope'),'平均正样本数':marked(b.get('positives_per_query'),b.get('positives_status')),
             '正样本口径':b.get('positives_scope'),'数量状态':b.get('status'),'长度状态':l.get('status','未统计'),
             '样本总数_去重':l.get('samples_dedup'),'token有效样本数':l.get('token_samples'),
             'word有效样本数':l.get('word_samples'),'缺失或冲突正文':l.get('missing_text'),
             '长度覆盖率':l.get('coverage'),'语言':l.get('language'),'排除非中英文样本数':l.get('excluded_other_language'),
             'tokenizer':l.get('tokenizer_path'),'tokenizer指纹':l.get('tokenizer_signature'),
             '长度口径':l.get('length_scope'),'长度去重身份':l.get('length_identity'),
             '备注':b.get('notes','')+('；'+l['notes'] if l.get('notes') else '')}
        for prefix in ('token','word'):
            row[prefix+'平均值']=l.get(prefix+'_mean')
            row[prefix+'平均值_多次']=l.get(prefix+'_mean_multi')
            for p in (25,50,75,90):
                row[prefix+'_P'+str(p)]=l.get(prefix+'_p'+str(p))
                row[prefix+'_P'+str(p)+'_多次']=l.get(prefix+'_p'+str(p)+'_multi')
        final.append(row)
        checks=['query数量','样本多少出多少','候选池数量','平均正样本数','token平均值','word平均值']
        checks += [prefix+'_P'+str(p) for prefix in ('token','word') for p in (25,50,75,90)]
        for field in checks:
            v=row.get(field)
            if v is None or v=='' or v=='未说明' or str(v).endswith('*'):
                issues.append({'数据集':row['数据集名称'],'config':suffix,'字段':field,'值':v,'原因':row['备注']})
        if row['数量状态']!='complete' or row['长度状态']!='complete':
            issues.append({'数据集':row['数据集名称'],'config':suffix,'字段':'就绪状态','值':row['数量状态']+'/'+row['长度状态'],'原因':row['备注']})
    return final,issues

def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--preview',type=int,default=8); args=parser.parse_args()
    manifest=load_manifest()
    write_summary(manifest)
    final,issues=build(manifest)
    if not final: print('先运行 count_basic.py'); return 2
    def write(name,rows):
        path=OUTPUTS_DIR/name; fields=list(dict.fromkeys(k for row in rows for k in row)) or ['数据集','config','字段','值','原因']
        with path.open('w',encoding='utf-8-sig',newline='') as handle:
            writer=csv.DictWriter(handle,fieldnames=fields); writer.writeheader(); writer.writerows(rows)
    write('final_stats.csv',final); write('issues.csv',issues)
    # 每个值都带同一个 config/子集标签，空值不省略，避免回填时错位。
    grouped={}
    for row in final:
        key=(row['记录类型'],row['数据集名称'])
        item=grouped.setdefault(key,{'记录类型':key[0],'数据集名称':key[1]})
        label=row['config']+('/'+row['子集'] if row['子集'] else '')
        for field,value in row.items():
            if field in ('记录类型','数据集名称','config','子集'): continue
            text=label+': '+(str(value) if value is not None and value!='' else '未统计')
            item[field]=(item[field]+' | '+text) if field in item else text
    write('by_record_row.csv',list(grouped.values()))
    banner('汇总')
    for r in final[:args.preview]: print(r['config'],r['子集'],r['query数量'],r['候选池数量'],r['token平均值'],r['长度状态'])
    print('主汇总 ../ir_data/outputs/final_stats.json；CSV 仅作导出')
    print('明细',len(final),'条；问题',len(issues),'条；输出 ../ir_data/outputs/')
    return 0
if __name__=='__main__': raise SystemExit(main())
