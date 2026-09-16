"""编排层 run_available 的"跳过 / 续跑 / 阻塞"判定测试。

只构造最小夹具，不跑真实统计、不读真实数据集、不需要 tokenizer。
边界与 stage2 的判定保持一致（同一个 fingerprint）。
"""
import argparse
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import run_available as r
import stage2 as s


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'data').mkdir()
        (self.root / 'data' / 'unit.jsonl').write_text('{"id":1,"initial_list":["a"]}\n', encoding='utf-8')
        (self.root / 'out' / 'experiments').mkdir(parents=True)
        (self.root / 'out' / 'cache').mkdir(parents=True)
        self.patches = [patch.object(r, 'OUTPUTS_DIR', self.root / 'out'),
                        patch.object(r, 'CACHE_DIR', self.root / 'out' / 'cache')]
        for item in self.patches:
            item.start()
        self.entry = {'path': str(self.root), 'language': ['en'], 'display_name': 'fixture',
                      'files': [{'path': 'data/unit.jsonl'}]}
        self.config = {'config_id': 'fx', 'split': 'train', 'papers': ['fixture'],
                       'sample_unit': 'passage',
                       'sample_source': {'kind': 'jsonl_ids', 'file': 'data/unit.jsonl',
                                         'id_field': 'initial_list'},
                       'query': {}, 'n_to_k': {'text': '1 -> 1'}}
        # configurations() 是从 entry['configs'] 里取配置的
        self.entry['configs'] = [self.config]
        self.manifest = {'datasets': {'fixture': self.entry}, 'globals': {}}
        self.stem = s.stem(self.config)
        self.sig = s.fingerprint(self.entry, self.config)

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def fabricate(self, basic=False, lengths=False, sqlite=False, measured=1):
        """造出"上一次运行留下的产物"。"""
        experiments = self.root / 'out' / 'experiments'
        cache = self.root / 'out' / 'cache'
        for path in list(experiments.glob('*.json')) + list(cache.glob('*')):
            path.unlink()
        if basic:
            (experiments / f'{self.stem}.json').write_text(
                json.dumps({'status': 'complete', 'fingerprint': self.sig}), encoding='utf-8')
        if lengths:
            (experiments / f'{self.stem}_lengths.json').write_text(
                json.dumps({'status': 'complete', 'fingerprint': self.sig,
                            'samples_measured': measured}), encoding='utf-8')
        if sqlite:
            sqlite3.connect(cache / f'{self.stem}.sqlite').close()

    def action(self):
        rows = r.build_plan(self.manifest, ['fixture'])
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_first_run_needs_both(self):
        self.fabricate()
        self.assertEqual(self.action()['action'], '数量+长度')

    def test_complete_is_skipped(self):
        self.fabricate(basic=True, lengths=True, sqlite=True)
        self.assertEqual(self.action()['action'], '跳过')

    def test_basic_done_runs_lengths_only(self):
        self.fabricate(basic=True, sqlite=True)
        self.assertEqual(self.action()['action'], '长度')

    def test_lost_candidate_cache_forces_basic_rerun(self):
        """数量与长度都标着 complete，但候选 sqlite 已丢失。

        这时若直接跑 lengths()，它会建一个空库、算出全空的指标，却写 status=complete，
        把好产物覆盖掉。所以必须拦住、回去重跑数量。
        """
        self.fabricate(basic=True, lengths=True, sqlite=False)
        row = self.action()
        self.assertEqual(row['action'], '数量+长度')
        self.assertIn('sqlite', row['reason'])

    def test_zero_measured_lengths_is_not_done(self):
        """长度结果 samples_measured=0 属于空产物，不能算完成。"""
        self.fabricate(basic=True, lengths=True, sqlite=True, measured=0)
        self.assertEqual(self.action()['action'], '长度')

    def test_declared_only_is_registered_not_run(self):
        """derived/external 这类只登记声明的配置，不能当成"没做完"反复重跑。"""
        self.config['sample_source'] = {'kind': 'external', 'file': 'data/unit.jsonl'}
        self.fabricate()
        self.assertEqual(self.action()['action'], '登记')

    def test_unknown_subset_is_declared_only(self):
        self.config['usage_scope'] = 'unknown_subset'
        self.fabricate()
        self.assertEqual(self.action()['action'], '登记')

    def test_missing_input_is_blocked(self):
        self.config['sample_source']['file'] = 'data/not-there.jsonl'
        self.fabricate()
        row = self.action()
        self.assertEqual(row['action'], '阻塞')
        self.assertIn('缺输入文件', row['detail'])

    def test_byte_mismatch_is_blocked(self):
        """字节数与 manifest 声明不符 = 还在上传，不能出正式统计。"""
        self.config['sample_source']['file'] = 'data/unit.jsonl'
        self.manifest['datasets']['fixture']['files'] = [
            {'path': 'data/unit.jsonl', 'bytes': 999999}]
        self.fabricate()
        row = self.action()
        self.assertEqual(row['action'], '阻塞')
        self.assertIn('应为', row['detail'])

    def test_fingerprint_change_invalidates_result(self):
        """样本文本改了（大小/内容变）→ 指纹变 → 已完成的也要重跑。"""
        self.fabricate(basic=True, lengths=True, sqlite=True)
        self.assertEqual(self.action()['action'], '跳过')
        (self.root / 'data' / 'unit.jsonl').write_text(
            '{"id":1,"initial_list":["a","b","c"]}\n', encoding='utf-8')
        self.assertEqual(self.action()['action'], '数量+长度')


class ProcessTests(unittest.TestCase):
    """process() 的执行流程与失败隔离。打桩 basic/lengths，不跑真实统计。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'out' / 'experiments').mkdir(parents=True)
        (self.root / 'out' / 'cache').mkdir(parents=True)
        self.patches = [patch.object(r, 'OUTPUTS_DIR', self.root / 'out'),
                        patch.object(r, 'CACHE_DIR', self.root / 'out' / 'cache')]
        for item in self.patches:
            item.start()
        self.config = {'config_id': 'fx', 'split': 'train', 'papers': ['fixture'],
                       'sample_unit': 'passage',
                       'sample_source': {'kind': 'jsonl_ids', 'file': 'data/unit.jsonl',
                                         'id_field': 'initial_list'},
                       'query': {}, 'n_to_k': {'text': '1 -> 1'}}
        self.entry = {'path': str(self.root), 'language': ['en'], 'display_name': 'fixture',
                      'files': [], 'configs': [self.config]}
        self.manifest = {'datasets': {'fixture': self.entry}, 'globals': {}}
        self.args = argparse.Namespace(force=False, lengths_only=False, limit=None,
                                       tokenizer=None, no_token=False)
        self.calls = []

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def row(self, action, basic=False, lengths=False):
        return {'dataset': 'fixture', 'config': 'fx', 'subset': '', 'stem': 'fx',
                'papers': 'fixture', 'split': 'train', 'action': action,
                'basic': basic, 'lengths': lengths, 'state': 'ready', 'detail': ''}

    def stub_basic(self, status='complete', error=None):
        def call(manifest, name, entry, config, force=False):
            if error:
                raise error
            self.calls.append('basic')
            return {'status': status, 'query_count': 1, 'pool_size': 1,
                    'pool_status': 'measured', 'positives_per_query': 0.5, 'notes': ''}
        return call

    def stub_lengths(self, status='complete'):
        def call(manifest, name, entry, config, limit=None, override=None, no_token=False):
            self.calls.append('lengths')
            return {'status': status, 'token_mean': 10.0, 'word_mean': 5.0,
                    'samples_measured': 1, 'samples_dedup': 1, 'notes': ''}
        return call

    def test_both_stages_run(self):
        with patch.object(r, 'basic', self.stub_basic()), patch.object(r, 'lengths', self.stub_lengths()):
            record = r.process(self.manifest, self.row('数量+长度'), self.args)
        self.assertEqual(self.calls, ['basic', 'lengths'])
        self.assertTrue(record['ok'])
        self.assertEqual(record['basic'], 'complete')
        self.assertEqual(record['lengths'], 'complete')

    def test_lengths_only_skips_basic(self):
        with patch.object(r, 'basic', self.stub_basic()), patch.object(r, 'lengths', self.stub_lengths()):
            r.process(self.manifest, self.row('长度', basic=True), self.args)
        self.assertEqual(self.calls, ['lengths'])

    def test_declared_only_does_not_run_lengths(self):
        with patch.object(r, 'basic', self.stub_basic()), patch.object(r, 'lengths', self.stub_lengths()):
            record = r.process(self.manifest, self.row('登记'), self.args)
        self.assertEqual(self.calls, ['basic'])
        self.assertIsNone(record['lengths'])

    def test_failure_is_captured_not_raised(self):
        """单条失败必须被记下来继续跑下一条，而不是把整轮打断。"""
        args = self.args
        with patch.object(r, 'basic', self.stub_basic(error=ValueError('fixture 坏了'))), \
                patch.object(r, 'lengths', self.stub_lengths()):
            record = r.process(self.manifest, self.row('数量+长度'), args)
        self.assertFalse(record['ok'])
        self.assertIn('fixture 坏了', record['error'])
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
