"""给老师看的总 JSON：数据集 → 论文设定 → split/子集 → 七项指标。"""
import copy
import csv
import json
import time

from _common import OUTPUTS_DIR
from stage2 import configurations, fingerprint, stem, write_json


def metric(value, status, explanation, display=None, **extra):
    return dict(value=value, status=status, display=display or (
        str(value) + ('*' if status == 'estimate' else '') if value is not None else explanation),
        explanation=explanation, **extra)


def task_context(name, config):
    if name == 'auxiliary_math':
        return '这是数学解题评测，每道题生成答案，不是检索或候选重排任务'
    if name == 'mteb_english':
        return '这是多个任务组成的 benchmark，须按具体 task 分开，不能给所有 task 编造统一的重排指标'
    if (config.get('query') or {}).get('count_status') == 'na':
        return '这是压缩器预训练或表示对齐语料，输入独立文本，没有 query 和候选排序列表'
    return '这是数据集发布方的检索基准，发布语料、query 和相关性标注，没有统一规定某个重排模型的输入输出数量'


def ranking_setting(name, config):
    spec=config.get('n_to_k') or {}
    if spec.get('status') == 'na':
        return metric(None, 'na', task_context(name,config)+'；因此没有固定的 N→K', n=None,k=None)
    text=spec.get('text') or '论文未说明输入候选数量和输出数量'
    explanation=spec.get('note') or '这是该论文的候选重排设定；N→N 表示对全部 N 个候选排序'
    if spec.get('n') is None and spec.get('k') is None:
        explanation += '；不能把多个设置合成一个数值'
    return metric(text, spec.get('status','unknown'), explanation, n=spec.get('n'), k=spec.get('k'))


def length_metric(prefix, config, basic, length, reason):
    value=length.get(prefix+'_mean')
    scope=basic.get('pool_scope') or config.get('pool_scope')
    reference=scope in ('full_corpus','author_pool')
    explanation=('按全部候选去重后的原文长度统计' if not reference else
                 '当前统计的是整个语料/大池子的原文长度，尚未取得该论文每个 query 的候选名单；这是参考长度，不能当作论文实际输入候选的精确长度')
    if prefix=='word':
        explanation+='；仅中英文样本进入均值和分位数，其他及未知语言单列并排除'
    if value is None:
        explanation=reason
    elif length.get('status') != 'complete':
        explanation+='；正文未全部覆盖，本次仅为已有正文的部分结果'
    status=('estimate' if reference else 'measured') if value is not None else 'unknown'
    if value is not None and length.get('status')!='complete': status='partial'
    return metric(value,status,explanation,
                  P25=length.get(prefix+'_p25'),P50=length.get(prefix+'_p50'),
                  P75=length.get(prefix+'_p75'),P90=length.get(prefix+'_p90'),
                  effective_samples=length.get(prefix+'_samples'),
                  reference_only=reference,
                  occurrence_weighted=dict(mean=length.get(prefix+'_mean_multi'),
                    **{'P'+str(p):length.get(prefix+'_p'+str(p)+'_multi') for p in (25,50,75,90)}))


def result_for(name, entry, config):
    basepath=OUTPUTS_DIR/'experiments'/(stem(config)+'.json')
    lengthpath=OUTPUTS_DIR/'experiments'/(stem(config)+'_lengths.json')
    b=json.loads(basepath.read_text(encoding='utf-8')) if basepath.exists() else {}
    l=json.loads(lengthpath.read_text(encoding='utf-8')) if lengthpath.exists() else {}
    stale=bool(b) and b.get('fingerprint')!=fingerprint(entry,config)
    if stale: b={}; l={}
    if l.get('fingerprint')!=b.get('fingerprint'): l={}
    declared=config.get('query') or {}
    context=task_context(name,config)
    unavailable=b.get('notes') or ('输入文件或配置已变化，需要重跑后更新统计' if stale else '该配置尚未完成服务器统计')
    q=b.get('query_count')
    qs=b.get('query_count_status','unknown')
    if q is None and declared.get('count') is not None:
        q=declared['count']; qs=declared.get('count_status','declared')
    if declared.get('count_status')=='na':
        query=metric(None,'na',context+'；所以没有 query 数量')
    else:
        explanation=('从该配置的实际 query 去重统计' if qs=='measured' else
                     '论文报告的使用规模，尚不能视为已从论文子集文件实测的 query 数量')
        if q is None: explanation=unavailable+'；因此尚不能确认 query 数量'
        if b.get('query_identity'): explanation+='；去重方式见 query_identity'
        query=metric(q,qs,explanation,paper_reported=declared.get('count'),
                     observed=b.get('query_count') if qs in ('measured','estimate') else None,
                     query_identity=b.get('query_identity'),data_records=b.get('training_record_count'))
    candidate=config.get('candidate') or {}
    pool=b.get('pool_size')
    ps=b.get('pool_status','unknown')
    scope=b.get('pool_scope') or config.get('pool_scope','candidate_union')
    if scope=='na' or candidate.get('pool_status')=='na':
        poolmetric=metric(None,'na',context+'；没有检索候选池')
    else:
        if pool is None and candidate.get('pool') is not None:
            pool=candidate['pool'];ps='estimate'
        explanation=('所有 query 的候选 doc ID 去重并集' if ps=='measured' else
                     '以整个语料、作者处理的大池子或无 doc ID 的正文并集代替精确候选 doc ID 并集，按老师要求标 *')
        if pool is None: explanation=unavailable+'；未取得精确候选并集或可确认的大池子数量'
        poolmetric=metric(pool,ps,explanation,scope=scope,
                          candidate_source=candidate.get('source'),doc_id_union_exact=ps=='measured')
    qrels=config.get('qrels') or {}
    positive=b.get('positives_per_query')
    positive_status=b.get('positives_status','unknown')
    if qrels.get('status')=='na':
        positives=metric(None,'na',context+'；没有 query–候选相关性正例统计')
    else:
        positive_scope=b.get('positives_scope') or config.get('positives_scope','annotated_pool')
        explanation=('按文件指定的正例统计；这是指定正例数量，不代表所有相关候选数量' if positive_scope=='designated_positive' else
                     '按相关性标注阈值筛选后，去重 query–doc 正例对数除以同一配置的 query 数；零正例 query 也进入分母')
        if positive is None:
            explanation=(qrels.get('note') or '未取得该论文配置的相关性标注；排序顺序或检索分数不能当作正例标签')+'；平均正样本数量暂不能计算'
        positives=metric(positive,positive_status,explanation,
                         positive_pairs=b.get('positives_total'),query_denominator=b.get('query_count'),
                         scope=positive_scope,threshold=qrels.get('positive_threshold'))
    length_reason=(l.get('notes') or unavailable)+'；因此该范围的平均长度和分位数尚未完成'
    if b.get('status')=='complete' and not l:
        length_reason='数量结果已落盘，但本配置的长度统计尚未完成；完成后由服务器流程更新'
    try: languages=json.loads(l.get('language') or '{}')
    except (ValueError,TypeError): languages={}
    scope_text=('该论文的实际公开候选文件' if scope=='candidate_union' else
                '整个语料/作者大池子参考范围；尚非该论文每个 query 的真实候选并集')
    if config.get('usage_scope')=='unknown_subset' or (config.get('sample_source') or {}).get('kind') in ('derived','external'):
        scope_text='仅记录论文声明；该论文实际抽样、候选或重新标注版本未取得，不能拿公开全集冒充'
    if config.get('usage_scope')=='public_release': scope_text='公开文件全集参考；不等于论文未公开的抽样子集或训练阶段划分'
    if name=='auxiliary_math': scope_text='题目原文长度参考，不是候选文本长度'
    return {'subset':config.get('_subset'), '统计范围':scope_text,
            '数量状态':b.get('status','pending'), '长度状态':l.get('status','pending'),
            '未完成原因':unavailable if b.get('status')!='complete' else (l.get('notes') if l.get('status')!='complete' else None),
            'query数量':query,'要求样本多少出多少':ranking_setting(name,config),
            '候选池数量':poolmetric,'每个query平均正样本数量':positives,
            '每个样本平均token':length_metric('token',config,b,l,length_reason),
            '每个样本平均word':length_metric('word',config,b,l,length_reason),
            '长度覆盖':{'去重样本数':l.get('samples_dedup'),'已计量样本数':l.get('samples_measured'),
                        '缺失或冲突正文':l.get('missing_text'),'覆盖率':l.get('coverage')},
            '语言':{'样本数':languages,'排除非中英文样本数':l.get('excluded_other_language'),
                    '说明':'其他或未知语言仍统计 token，但不计 word'},
            '原始结果文件':{'数量':'../ir_data/outputs/experiments/'+basepath.name,
                           '长度':'../ir_data/outputs/experiments/'+lengthpath.name},
            '备注':b.get('notes','')+('；'+l['notes'] if l.get('notes') else '')}


def paper_notes(name, paper, config):
    notes=[config.get('note'),(config.get('n_to_k') or {}).get('note'),
           (config.get('sample_source') or {}).get('note')]
    if name=='beir':
        if any(x in paper for x in ('Compress-then-Rank','PE-Rank','FullRank')):
            notes.append('该论文采用 window 20 / step 10；这是内部排序窗口，整个 query 的输入输出数量另见 N→K')
        if 'REARANK' in paper: notes.append('该论文采用 window 20、10 次迭代；整个 query 的输入输出数量另见 N→K')
    # 共用配置原始备注可能混合多篇论文；只展示本论文可确认的设置。
    return [n for n in notes if n and not (name=='beir' and
            ('C2R / PE-Rank / FullRank' in n or 'REARANK 以 window' in n))]


def build_summary(manifest):
    inventory={}
    inventorypath=OUTPUTS_DIR/'inventory.csv'
    if inventorypath.exists():
        with inventorypath.open(encoding='utf-8-sig',newline='') as f:
            for row in csv.DictReader(f): inventory.setdefault(row['dataset'],[]).append(row)
    datasets={}
    for name,entry in manifest['datasets'].items():
        expanded=list(configurations(manifest,[name]))
        configs=[]
        for original in entry.get('configs',[]):
            variants=[c for _,_,c in expanded if c['config_id']==original['config_id']]
            # 即使共用同一批底层统计，各论文也有独立的、可读的设定与结果。
            for paper in original.get('papers') or ['数据集发布方（论文名称待核实）']:
                item={'config_id':original['config_id'],'paper':paper,
                    'paper_role':original.get('paper_role'),'split':original.get('split'),
                    'sample_unit':original.get('sample_unit'),
                    '样本单位说明':{'passage':'一段候选文本','document':'一篇候选文档','paper':'一篇候选论文',
                                      'question':'一道数学题（辅助评测）','varies':'随具体 task 变化，须逐 task 统计'}.get(original.get('sample_unit'),'按论文定义的候选输入单位'),
                    'usage_scope':original.get('usage_scope','paper_setting'),
                    '处理方式说明':paper_notes(name,paper,original),
                    '实验设定':{'样本多少出多少':ranking_setting(name,original),
                               '候选来源':(original.get('candidate') or {}).get('source') or task_context(name,original),
                               'query设置':copy.deepcopy(original.get('query') or {}),
                               '相关性标注设置':copy.deepcopy(original.get('qrels') or {}),
                               '样本文件与解析方式':copy.deepcopy(original.get('sample_source') or {})},
                    'results':[result_for(name,entry,c) for c in variants]}
                if name=='fullrank_training_data' and original['config_id']=='fullrank-train-1k':
                    # 原定义将 RankMistral20/100 放在一个 text 中。给老师分别列出，
                    # 不把公开 top100 文件冒充缺失的 top20/其他教师版本。
                    for n in (20,100):
                        variant=copy.deepcopy(item)
                        variant['source_config_id']=original['config_id']
                        variant['config_id']=original['config_id']+'-rankmistral'+str(n)
                        variant['设定名称']='RankMistral'+str(n)+'（论文设定，具体教师版本须进一步核实）'
                        setting=metric(f'{n} -> {n}','declared',f'论文的 RankMistral{n} 全排序设置；公开 top100 文件只代表文件名对应的一个教师版本',n=n,k=n)
                        variant['实验设定']['样本多少出多少']=setting
                        for result in variant['results']: result['要求样本多少出多少']=copy.deepcopy(setting)
                        configs.append(variant)
                else:
                    configs.append(item)
        datasets[name]={'display_name':entry.get('display_name',name),'path':entry.get('path'),
            'medical':entry.get('medical',False),'数据来源':entry.get('source'),
            '文件检查':inventory.get(name,[]),'configs':configs}
    return {'schema_version':3,'updated':time.strftime('%Y-%m-%d %H:%M:%S'),
        '说明':['这是给老师看的主汇总；datasets 按数据集组织，configs 按论文方法与实验设定区分，results 按 split/子集区分。',
              '数字附带 value、status、display 和 explanation；未计算、未公开、没有相应任务必须分别解释。',
              '候选池非精确 doc ID 并集标 *；全语料参考长度也标 *，不能冒充论文实际输入候选长度。',
              '论文声明与公开文件实测分别记录；公开全集参考不能作为未公开论文子集的精确结果。',
              '第 7 项的 token 和 word P25/P50/P75/P90，分别放在第 5、6 项对象中。'],
        '统一统计规则':copy.deepcopy(manifest.get('globals',{})),
        'datasets':datasets,'未公开派生数据说明':copy.deepcopy(manifest.get('unpublished',[])),
        '证据仓库':copy.deepcopy(manifest.get('reference_repos',manifest.get('references',[])))}


def write_summary(manifest):
    summary=build_summary(manifest)
    write_json(OUTPUTS_DIR/'final_stats.json',summary)
    return summary
