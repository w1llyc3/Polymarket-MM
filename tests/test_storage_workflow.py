import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch

from nba_recorder.storage_workflow import plan, compress_one, lock, sha256, after_research


class StorageWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.folder = self.root / 'data/research/test/L0'
        self.folder.mkdir(parents=True)
        self.db = self.folder / 'paper.sqlite'
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('CREATE TABLE evidence(payload)')
            db.executemany('INSERT INTO evidence VALUES(?)', [('unchanged' * 1000,)] * 100)
        (self.folder / 'result.json').write_text(json.dumps({'shutdown_ledger_consistent': True, 'real_submission_calls': 0}))

    def tearDown(self):
        self.tmp.cleanup()

    def test_scope_preserves_reports_shared_inputs_and_raw(self):
        for name in ('source-tape.sqlite', 'shared-inputs.sqlite', 'result.csv', 'chart.png', 'index.html'):
            (self.folder / name).touch()
        raw = self.root / 'data/console'
        raw.mkdir(); (raw / 'paper.sqlite').touch()
        self.assertEqual([i['path'] for i in plan(self.root)['items']], ['data/research/test/L0/paper.sqlite'])

    def test_unfinished_or_wal_is_skipped(self):
        wal = Path(str(self.db) + '-wal'); wal.write_bytes(b'pending')
        self.assertFalse(plan(self.root)['items'])
        wal.unlink()
        (self.folder / 'result.json').write_text('{}')
        self.assertFalse(plan(self.root)['items'])

    def test_active_writer_is_refused(self):
        if os.name != 'nt': self.skipTest('Windows compression')
        with lock(Path(str(self.db) + '.writer.lock')):
            with self.assertRaises(OSError):
                compress_one(self.root, plan(self.root)['items'][0])

    @unittest.skipUnless(os.name == 'nt', 'Windows NTFS')
    def test_real_transparent_compression_keeps_sqlite_and_exact_bytes(self):
        before = sha256(self.db)
        result = compress_one(self.root, plan(self.root)['items'][0])
        self.assertEqual(before, sha256(self.db))
        self.assertLess(result['allocated_after'], result['allocated_before'])
        with closing(sqlite3.connect(self.db.as_uri() + '?mode=ro', uri=True)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM evidence').fetchone()[0], 100)
        self.assertEqual(len(plan(self.root)['items']), 1)

    def test_policy_default_is_no_mutation(self):
        with patch('nba_recorder.storage_workflow.apply') as apply:
            after_research(self.root, self.folder)
        apply.assert_not_called()

    def test_enabled_policy_routes_only_completed_output(self):
        (self.root/'data/storage-policy.json').write_text('{"compress_completed_research":true}')
        with patch('nba_recorder.storage_workflow.apply', return_value=(self.root/'report.json', {'errors': []})) as apply:
            after_research(self.root, self.folder)
        apply.assert_called_once_with(self.root, self.folder)

    def test_enabled_policy_failure_keeps_research_and_evidence(self):
        (self.root/'data/storage-policy.json').write_text('{"compress_completed_research":true}')
        before = sha256(self.db)
        with patch('nba_recorder.storage_workflow.apply', side_effect=OSError('injected')):
            after_research(self.root, self.folder)
        self.assertEqual(before, sha256(self.db))
        self.assertIn('injected', (self.folder/'storage-maintenance-error.json').read_text())

    def test_path_escape_refused(self):
        with self.assertRaises(ValueError):
            plan(self.root, self.root.parent)

    @unittest.skipUnless(os.name == 'nt', 'Windows compression')
    def test_mutation_or_command_failure_is_visible_without_deletion(self):
        def mutate(*args, **kwargs):
            with self.db.open('ab') as fp: fp.write(b'changed')
            return type('Result', (), {'returncode': 0})()
        with patch('nba_recorder.storage_workflow.subprocess.run', side_effect=mutate):
            with self.assertRaisesRegex(RuntimeError, 'Content changed'):
                compress_one(self.root, plan(self.root)['items'][0])
        self.assertTrue(self.db.exists())


if __name__ == '__main__':
    unittest.main()
