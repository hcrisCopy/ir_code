"""在服务器验证交付结构和口径，避免表格丢掉论文设定。"""
import json
import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import stage2
import summarize_results as summary


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.patch=patch.object(summary,'OUTPUTS_DIR',self.root/'outputs');self.patch.start()
        self.config={'config_id':'test','papers':['Method A','Method B'],'paper_role':'method',
                     'split':'test','sample_unit':'document','query':{'count':43,'count_status':'declared'},
                     'n_to_k':{'n':100,'k':100,'text':'100 -> 100','status':'declared'},
                     'sample_source':{'kind':'jsonl','file':'missing.jsonl'},'pool_scope':'full_corpus'}
        self.entry={'path':str(self.root),'display_name':'test','configs':[self.config]}
        self.manifest={'datasets':{'test':self.entry},'globals':{}}
    def tearDown(self):
        self.patch.stop();self.temp.cleanup()
    def test_one_entry_per_paper_and_all_seven_metrics(self):
        result=summary.build_summary(self.manifest)
        configs=result['datasets']['test']['configs']
        self.assertEqual([c['paper'] for c in configs],['Method A','Method B'])
        row=configs[0]['results'][0]
        self.assertEqual(row['query数量']['value'],43)
        self.assertEqual(row['query数量']['status'],'declared')
        self.assertIsNone(row['query数量']['observed'])
        self.assertEqual(row['要求样本多少出多少']['n'],100)
        self.assertEqual(set(row['每个样本平均token']) & {'P25','P50','P75','P90'}, {'P25','P50','P75','P90'})
        self.assertIn('每个query平均正样本数量',row)
    def test_actual_and_declared_and_starred_reference_are_distinct(self):
        p=self.root/'outputs/experiments';p.mkdir(parents=True)
        sig=stage2.fingerprint(self.entry,self.config)
        b={'fingerprint':sig,'status':'complete','query_count':42,'query_count_status':'measured',
           'pool_size':1000,'pool_status':'estimate','pool_scope':'full_corpus'}
        l={'fingerprint':sig,'status':'complete','token_mean':23,'token_p25':10,'token_p50':20,
           'token_p75':30,'token_p90':40,'token_samples':1000}
        (p/'test.json').write_text(json.dumps(b));(p/'test_lengths.json').write_text(json.dumps(l))
        row=summary.result_for('test',self.entry,self.config)
        self.assertEqual(row['query数量']['value'],42)
        self.assertEqual(row['query数量']['paper_reported'],43)
        self.assertEqual(row['候选池数量']['display'],'1000*')
        self.assertEqual(row['每个样本平均token']['display'],'23*')
        self.assertTrue(row['每个样本平均token']['reference_only'])
    def test_no_bare_not_applicable_for_auxiliary_tasks(self):
        self.config['query']={'count':30,'count_status':'declared'}
        self.config['n_to_k']={'status':'na','text':'不适用'}
        self.config['candidate']={'pool_status':'na'}
        self.config['qrels']={'status':'na'}
        row=summary.result_for('auxiliary_math',self.entry,self.config)
        for key in ('要求样本多少出多少','候选池数量','每个query平均正样本数量'):
            self.assertIn('数学解题',row[key]['display'])
            self.assertNotEqual(row[key]['display'],'不适用')
    def test_fullrank_paper_settings_split_without_claiming_public_subset(self):
        self.config['config_id']='fullrank-train-1k'
        self.config['papers']=['FullRank'];self.config['usage_scope']='unknown_subset'
        self.manifest['datasets']={'fullrank_training_data':self.entry}
        rows=summary.build_summary(self.manifest)['datasets']['fullrank_training_data']['configs']
        self.assertEqual([c['实验设定']['样本多少出多少']['n'] for c in rows],[20,100])
        self.assertTrue(all(c['results'][0]['数量状态']=='pending' for c in rows))
    def test_pending_upload_is_explained_without_running_statistics(self):
        (self.root/'data.jsonl').write_text('partial')
        self.entry['files']=[{'path':'data.jsonl','bytes':100}]
        self.config['sample_source']={'kind':'jsonl','file':'data.jsonl'}
        row=summary.result_for('test',self.entry,self.config)
        self.assertIn('等待上传完整',row['未完成原因'])
        self.assertIsNone(row['每个样本平均token']['value'])
    def test_ranking_audit_preserves_statistical_fingerprint(self):
        before=stage2.fingerprint(self.entry,self.config)
        audited=copy.deepcopy(self.config)
        audited['statistics_metadata_before_ranking_audit']=copy.deepcopy(audited['n_to_k'])
        audited['n_to_k']={'n':100,'k':None,'status':'unknown','text':'输入100个，输出待核实'}
        self.assertEqual(before,stage2.fingerprint(self.entry,audited))
    def test_unknown_output_never_becomes_n_by_default(self):
        self.config['n_to_k']={'n':200,'k':None,'status':'unknown','text':'输入200个，输出数量待核实',
                               'output_status':'unknown','final_keep':{'value':None,'status':'unknown'}}
        setting=summary.ranking_setting('test',self.config)
        self.assertEqual(setting['n'],200)
        self.assertIsNone(setting['k'])
        self.assertEqual(setting['output_status'],'unknown')
        self.assertIsNone(setting['final_keep']['value'])
    def test_paper_specific_output_and_single_call_do_not_mix(self):
        self.config['n_to_k']={'n':100,'k':None,'status':'unknown','per_paper':{
            'Method A':{'n':100,'k':10,'status':'declared','text':'100 -> 10'},
            'Method B':{'n':100,'k':100,'status':'declared','text':'100 -> 100',
                         'single_call':{'input_candidates':20,'output_candidates':20}}}}
        configs=summary.build_summary(self.manifest)['datasets']['test']['configs']
        self.assertEqual([c['results'][0]['要求样本多少出多少']['k'] for c in configs],[10,100])
        self.assertEqual(configs[1]['results'][0]['要求样本多少出多少']['single_call']['input_candidates'],20)


if __name__=='__main__':unittest.main()
