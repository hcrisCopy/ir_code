"""仅在服务器运行的统计边界测试，不读取真实数据集。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import stage2 as s
from _common import percentile_nearest


class StatisticsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.patches=[patch.object(s,'CACHE_DIR',self.root/'cache'),patch.object(s,'OUTPUTS_DIR',self.root/'outputs')]
        for p in self.patches:p.start()
        self.entry={'path':str(self.root),'language':['en'],'display_name':'fixture','files':[]}
        self.config={'config_id':'test','split':'train','papers':['fixture'],
                     'sample_unit':'passage','sample_source':{'kind':'jsonl_ids','file':'data.jsonl','id_field':'initial_list'},
                     'query':{},'n_to_k':{'text':'2 -> 2'},'positive_field':'relevant_docids'}
        self.manifest={'datasets':{'fixture':self.entry},'globals':{'tokenizer':{'batch_size':2},'word_rule':{}}}
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.temp.cleanup()
    def write(self,rows):
        (self.root/'data.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in rows))
    def basic(self):return s.basic(self.manifest,'fixture',self.entry,self.config)
    def test_unique_queries_and_positive_pairs(self):
        self.write([{'qid':0,'initial_list':['a','b'],'relevant_docids':['a']},
                    {'qid':0,'initial_list':['a','b'],'relevant_docids':['a']},
                    {'qid':1,'initial_list':['a','c'],'relevant_docids':[]}])
        r=self.basic(); self.assertEqual(r['query_count'],2); self.assertEqual(r['pool_size'],3)
        self.assertEqual(r['positives_total'],1); self.assertEqual(r['positives_per_query'],.5)
    def test_corpus_is_starred_and_json_qrels_fields(self):
        self.config.pop('positive_field'); self.config['sample_source']={'kind':'jsonl','file':'data.jsonl','id_field':'id','text_fields':['text']}
        self.config['query']={'file':'queries.jsonl','id_field':'id'}
        self.config['qrels']={'file':'qrels.jsonl','fields':{'qid':'q_id','docid':'p_id','rel':'score'},'positive_threshold':'>0'}
        self.write([{'id':'a','text':'alpha'},{'id':'b','text':'beta'}])
        (self.root/'queries.jsonl').write_text('{"id":"0"}\n{"id":"1"}\n')
        (self.root/'qrels.jsonl').write_text('{"q_id":"0","p_id":"a","score":1}\n{"q_id":"1","p_id":"b","score":0}\n')
        r=self.basic(); self.assertEqual(r['pool_status'],'estimate'); self.assertEqual(r['positives_per_query'],.5)
    def test_language_labels(self):
        e={'language':['zh','en','fr']}
        self.assertEqual(s.language(e,'miracl_fr'),'fr')
        self.assertEqual(s.language(e,'miracl_ja'),'ja')
        self.assertEqual(s.language(e,'cMedQAv2'),'zh')
        self.assertEqual(s.language(e,'unknown-source'),'unknown')
    def test_percentiles_nearest_rank(self):
        self.assertEqual(percentile_nearest([1,2,3,4],25),1)
        self.assertEqual(percentile_nearest([1,2,3,4],90),4)
    def test_no_proxy_for_unknown_subset(self):
        self.write([{'qid':0,'initial_list':['a'],'relevant_docids':['a']}])
        self.config['usage_scope']='unknown_subset';self.config['query']={'count':99,'count_status':'declared'}
        r=self.basic();self.assertEqual(r['query_count'],99);self.assertIsNone(r['pool_size']);self.assertEqual(r['status'],'unavailable')
    def test_truncated_file_rejected(self):
        (self.root/'data.jsonl').write_text('{"qid":')
        self.assertEqual(self.basic()['status'],'unavailable')
    def test_length_dedup_weighting_and_zero_qid(self):
        self.config.pop('positive_field');self.config['sample_source']={'kind':'jsonl_list','file':'data.jsonl','unit_field':'document'}
        self.write([{'qid':0,'document':['a','abc']},{'qid':0,'document':['a','abc']}])
        b=self.basic();self.assertEqual(b['query_count'],1)
        with patch.object(s,'tokenizer_signature',return_value=('testsig','fixture')),patch.object(s,'load_tokenizer',return_value=object()),patch.object(s,'count_tokens',side_effect=lambda t,texts,a:[len(x) for x in texts]):
            r=s.lengths(self.manifest,'fixture',self.entry,self.config)
        self.assertEqual(r['token_samples'],2);self.assertEqual(r['token_mean'],2)
        self.assertEqual(r['token_p25'],1);self.assertEqual(r['token_p75'],3)
        self.assertEqual(r['samples_total'],4)
    def test_conversation_excludes_trailing_instruction(self):
        row={'conversations':[{'from':'human','value':'header\n[1] alpha\n[2] beta\nSearch Query: q\nRank the 2 passages above.'}]}
        c={'sample_source':{'kind':'conversation','per_query':2}}
        docs=list(s.documents(row,c));self.assertEqual(docs[-1][1],'beta')
    def test_ijson_mapping_stream(self):
        p=self.root/'mapping.json';p.write_text('{"a":"alpha","b":"beta"}')
        self.assertEqual(list(s.rows_of(p)),[{'id':'a','text':'alpha'},{'id':'b','text':'beta'}])

if __name__=='__main__':unittest.main()
