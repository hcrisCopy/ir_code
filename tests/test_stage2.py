"""仅在服务器运行的统计边界测试，不读取真实数据集。"""
import json
import gzip
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import stage2 as s
from _common import percentile_nearest
from extract_data import process_archive
from check_data import check_dataset


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
    def test_qrels_failure_keeps_measured_query_and_pool(self):
        """qrels 解析失败不能陪葬已实测的 query 数与候选池（P1-2）。"""
        self.config.pop('positive_field'); self.config['sample_source']={'kind':'jsonl','file':'data.jsonl','id_field':'id','text_fields':['text']}
        self.config['query']={'file':'queries.jsonl','id_field':'id'}
        # 默认字段 0/2/3 要求 4 列；实际文件只有 3 列 -> 必然解析失败
        self.config['qrels']={'file':'qrels.tsv','positive_threshold':'>0'}
        self.write([{'id':'a','text':'alpha'},{'id':'b','text':'beta'}])
        (self.root/'queries.jsonl').write_text('{"id":"0"}\n{"id":"1"}\n')
        (self.root/'qrels.tsv').write_text('0\ta\t1\n0\tb\t0\n')
        r=self.basic()
        self.assertEqual(r['status'],'complete')
        self.assertEqual(r['query_count'],2); self.assertEqual(r['query_count_status'],'measured')
        self.assertEqual(r['pool_size'],2)
        self.assertEqual(r['positives_status'],'unknown'); self.assertIsNone(r['positives_total'])
        self.assertIn('正例统计失败',r['notes']); self.assertIn('qrels.tsv',r['notes'])
    def test_missing_qrels_file_noted_but_complete(self):
        """qrels 声明了但文件缺失：其余指标照常，备注写明，等文件就位后指纹变化自动重算。"""
        self.config.pop('positive_field'); self.config['sample_source']={'kind':'jsonl','file':'data.jsonl','id_field':'id','text_fields':['text']}
        self.config['query']={'file':'queries.jsonl','id_field':'id'}
        self.config['qrels']={'file':'qrels.tsv','positive_threshold':'>0'}
        self.write([{'id':'a','text':'alpha'},{'id':'b','text':'beta'}])
        (self.root/'queries.jsonl').write_text('{"id":"0"}\n')
        r=self.basic()
        self.assertEqual(r['status'],'complete'); self.assertEqual(r['query_count'],1)
        self.assertEqual(r['positives_status'],'unknown')
        self.assertIn('qrels 文件缺失',r['notes'])
    def test_qrels_error_names_file_and_row(self):
        """报错信息必须带文件名与行号，方便逐数据集排查。"""
        self.config.pop('positive_field'); self.config['sample_source']={'kind':'jsonl','file':'data.jsonl','id_field':'id','text_fields':['text']}
        self.config['query']={'file':'queries.jsonl','id_field':'id'}
        self.config['qrels']={'file':'qrels.tsv','fields':{'qid':0,'docid':1,'rel':2},'positive_threshold':'>0'}
        self.write([{'id':'a','text':'alpha'}])
        (self.root/'queries.jsonl').write_text('{"id":"0"}\n')
        (self.root/'qrels.tsv').write_text('0\ta\t1\n0\tb\ttwo\n')
        r=self.basic()
        self.assertIn('qrels.tsv 第 2 行',r['notes'])
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
    def test_gzip_partial_output_is_rebuilt_before_deletion(self):
        p=self.root/'text.gz'
        with gzip.open(p,'wb') as h:h.write(b'complete body')
        (self.root/'text').write_bytes(b'com')
        r=process_archive(p,self.root,{'extract':'gz'},False,False)
        self.assertTrue(r['ok']);self.assertFalse(p.exists())
        self.assertEqual((self.root/'text').read_bytes(),b'complete body')
        entry={'path':str(self.root),'files':[{'path':'text.gz','extract':'gz'}]}
        rows,_=check_dataset('fixture',entry);self.assertEqual(rows[0]['status'],'ok')
    def test_incomplete_shard_set_not_ready(self):
        (self.root/'train-0.parquet').write_bytes(b'fixture')
        entry={'path':str(self.root),'files':[{'path':'train-*.parquet','min_files':2}]}
        rows,_=check_dataset('fixture',entry);self.assertEqual(rows[0]['status'],'missing')
    def test_unknown_language_excluded_from_word(self):
        self.entry['language']=['en','fr']
        self.config.pop('positive_field');self.config['sample_source']={'kind':'jsonl_list','file':'data.jsonl','unit_field':'document'}
        self.write([{'qid':0,'source':'miracl_fr','document':['bonjour']},{'qid':1,'source':'miracl_en','document':['hello']}])
        self.basic()
        with patch.object(s,'tokenizer_signature',return_value=('testsig','fixture')),patch.object(s,'load_tokenizer',return_value=object()),patch.object(s,'count_tokens',side_effect=lambda t,texts,a:[len(x) for x in texts]):
            r=s.lengths(self.manifest,'fixture',self.entry,self.config)
        self.assertEqual(r['token_samples'],2);self.assertEqual(r['word_samples'],1)
        self.assertEqual(r['excluded_other_language'],1)


class FingerprintTests(unittest.TestCase):
    """指纹必须跨机器稳定：内容不变，只动 mtime，指纹不能变。

    统计流程是"服务器上算、结果拿回本地汇总"。旧实现把 st_mtime_ns 放进指纹，
    导致服务器算好的结果在本地全部被判过期（summarize_results.result_for 的 stale
    判定会把 query/候选池/正例/长度整体清空）。这里锁定修复后的行为。
    """

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.entry={'path':str(self.root),'language':['en'],'display_name':'fixture','files':[]}
        self.config={'config_id':'fp','split':'train','papers':['fixture'],
                     'sample_unit':'passage','sample_source':{'kind':'jsonl_ids',
                     'file':'data.jsonl','id_field':'initial_list'},'query':{},'n_to_k':{'text':'1 -> 1'}}
        (self.root/'data.jsonl').write_text('{"id":1,"initial_list":["a"]}\n'*50,encoding='utf-8')

    def tearDown(self):
        self.temp.cleanup()

    def test_mtime_change_keeps_fingerprint(self):
        """核心回归：内容一字不改，只 utime 改时间戳 → 指纹必须不变。"""
        before=s.fingerprint(self.entry,self.config)
        old=os.stat(self.root/'data.jsonl').st_mtime_ns
        os.utime(self.root/'data.jsonl',ns=(old-777777777,old-777777777))
        # 清掉进程内缓存，确保真的重算内容哈希而不是吃缓存
        s._CONTENT_SIGNATURE_CACHE.clear()
        self.assertEqual(before,s.fingerprint(self.entry,self.config))

    def test_content_change_at_head_breaks_fingerprint(self):
        before=s.fingerprint(self.entry,self.config)
        (self.root/'data.jsonl').write_text('{"id":0,"initial_list":["a"]}\n'+'{"id":1,"initial_list":["a"]}\n'*49,encoding='utf-8')
        self.assertNotEqual(before,s.fingerprint(self.entry,self.config))

    def test_content_change_at_tail_breaks_fingerprint(self):
        """文件超过 64 KiB 时，尾部变化也必须被首尾哈希抓到。"""
        big='x'*200
        (self.root/'data.jsonl').write_text(('{"id":1,"initial_list":["%s"]}\n'%big)*400,encoding='utf-8')
        self.assertGreater((self.root/'data.jsonl').stat().st_size,64*1024)
        before=s.fingerprint(self.entry,self.config)
        # 只改最后一行的内容，总大小保持一致
        lines=(self.root/'data.jsonl').read_text(encoding='utf-8').splitlines(True)
        lines[-1]='{"id":1,"initial_list":["%s"]}\n'%('y'*200)
        (self.root/'data.jsonl').write_text(''.join(lines),encoding='utf-8')
        s._CONTENT_SIGNATURE_CACHE.clear()
        self.assertNotEqual(before,s.fingerprint(self.entry,self.config))

    def test_size_change_breaks_fingerprint(self):
        before=s.fingerprint(self.entry,self.config)
        (self.root/'data.jsonl').write_text('{"id":1,"initial_list":["a"]}\n',encoding='utf-8')
        self.assertNotEqual(before,s.fingerprint(self.entry,self.config))


if __name__=='__main__':unittest.main()
