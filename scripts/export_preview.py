"""导出已完成的部分结果用于展示，只整理 JSON，不重新编码。"""
import argparse
import copy
import json
from pathlib import Path
import time

from _common import OUTPUTS_DIR
from stage2 import write_json


def cell(value):
    return '—' if value is None else str(value).replace('|','／').replace('\n',' ')


def table(lines, headers, rows):
    lines += ['| '+' | '.join(headers)+' |','| '+' | '.join('---' for _ in headers)+' |']
    lines.extend('| '+' | '.join(cell(v) for v in row)+' |' for row in rows)
    lines.append('')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset',nargs='+',default=['r2med','rearank_12k'])
    args=parser.parse_args()
    source=OUTPUTS_DIR/'final_stats.json'
    summary=json.loads(source.read_text(encoding='utf-8'))
    unknown=set(args.dataset)-summary['datasets'].keys()
    if unknown: parser.error('总 JSON 中没有数据集：'+','.join(sorted(unknown)))
    selected=copy.deepcopy(summary)
    selected['datasets']={}
    selected['展示说明']='只提取数量与长度已完成的配置；未取得的正例或最终保留数仍明确标未知，参考范围仍标星，不把“完成编码”当作“所有七项都精确”。'
    selected['exported_at']=time.strftime('%Y-%m-%d %H:%M:%S')
    lines=['# 第二阶段：已完成部分结果','',
           '本展示从正式总 JSON 提取；没有抽样，没有重新编码。每种论文设定分别展示。',
           '候选池 `*` 表示全语料/大池子或正文哈希并集参考；长度 `*` 表示全语料参考长度。',
           'N/M 是候选输入和要求输出；完整排序长度、单次窗口与最终保留数量分别说明。',
           'Token 使用 Qwen3-1.7B，不截断、无特殊 token；word 仅中英文，分位数按最近秩法。','']
    for name in args.dataset:
        dataset=copy.deepcopy(summary['datasets'][name]);configs=[]
        for config in dataset['configs']:
            config['results']=[r for r in config['results']
                if r['数量状态']=='complete' and r['长度状态']=='complete']
            if not config['results']: continue
            configs.append(config)
            lines += ['## '+dataset['display_name']+' / '+config['paper'],'',
                      '设置：'+config['实验设定']['样本多少出多少']['display']+'。',
                      '样本单位：'+config['样本单位说明']+'；split：'+str(config['split'])+'。',
                      '统计范围：'+config['results'][0]['统计范围']+'。','']
            rows=config['results']
            table(lines,['子集','query 数','公开数据记录数','候选池','平均正样本数'],[
                [r.get('subset') or '全部',r['query数量']['display'],r['query数量'].get('data_records'),
                 r['候选池数量']['display'],r['每个query平均正样本数量']['value']
                    if r['每个query平均正样本数量']['value'] is not None else '未取得相关性标签'] for r in rows])
            for field,label in [('每个样本平均token','token'),('每个样本平均word','word')]:
                lines += ['### '+label+' 长度','']
                table(lines,['子集','平均值','P25','P50','P75','P90','有效样本数'],[
                    [r.get('subset') or '全部',r[field]['display']]+[
                        cell(r[field].get('P'+str(p)))+('*' if r[field].get('reference_only') and r[field].get('P'+str(p)) is not None else '')
                        for p in (25,50,75,90)]+[r[field].get('effective_samples')] for r in rows])
            if any(r['每个query平均正样本数量']['value'] is None for r in rows):
                lines += ['正样本缺项：'+rows[0]['每个query平均正样本数量']['explanation']+'。','']
            if rows[0]['query数量'].get('paper_reported') not in (None,rows[0]['query数量']['value']):
                lines += ['论文报告 query 数为 '+str(rows[0]['query数量']['paper_reported'])+
                          '，公开文件实测为 '+str(rows[0]['query数量']['value'])+'；本次使用实测分母。','']
        if configs:
            dataset['configs']=configs;selected['datasets'][name]=dataset
    if not selected['datasets']: parser.error('指定范围尚无数量和长度都完成的配置')
    target=OUTPUTS_DIR/'previews'
    write_json(target/'ready_results.json',selected)
    (target/'ready_results.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print('已完成部分展示：../ir_data/outputs/previews/ready_results.json / ready_results.md')
    print('数据组：'+', '.join(selected['datasets']))


if __name__=='__main__': main()
