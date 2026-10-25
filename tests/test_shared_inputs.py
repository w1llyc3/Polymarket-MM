import copy
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
import threading

from nba_recorder.fair_value.store import read_connection
from nba_recorder.inplay.demo import events, AT
from nba_recorder.inplay.manager import UnifiedPaperManager
from nba_recorder.inplay.store import SingleWriterStore
from nba_recorder.research.day_study import IndexedHistoricalSession


class SharedInputTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.pool = self.root / 'shared-inputs.sqlite'

    def tearDown(self):
        self.tmp.cleanup()

    def replay(self, name, shared, model='L2'):
        path = self.root / name / 'paper.sqlite'
        store = SingleWriterStore(path, shared_inputs=self.pool if shared else None)
        try:
            om = UnifiedPaperManager(store, start_ms=AT)
            session = IndexedHistoricalSession(om, model)
            for event in events(): session.on_event(copy.deepcopy(event))
            result = {
                'account': om.account(),
                'inputs': [tuple(r) for r in store.db.execute('SELECT rowid,id,time_ms,payload FROM ip_inputs ORDER BY time_ms,rowid')],
                'orders': [tuple(r) for r in store.db.execute('SELECT * FROM pe_orders ORDER BY id')],
                'fills': [tuple(r) for r in store.db.execute('SELECT * FROM pe_fills ORDER BY rowid')],
            }
            self.assertTrue(om.consistent())
            return path, result
        finally:
            store.close()

    def test_exact_replay_equivalence_and_three_models_share_content(self):
        _, legacy = self.replay('legacy', False)
        path, shared = self.replay('L2', True)
        self.assertEqual(legacy, shared)
        for model in ('L0', 'L1'):
            _, old = self.replay('legacy-' + model, False, model)
            _, new = self.replay(model, True, model)
            self.assertEqual(old, new)
        with closing(sqlite3.connect(self.pool)) as pool:
            self.assertEqual(pool.execute('SELECT count(*) FROM inputs').fetchone()[0], len(shared['inputs']))
            with self.assertRaises(sqlite3.IntegrityError):
                pool.execute("UPDATE inputs SET payload=x'00'")
        with closing(read_connection(path)) as db:
            actual = [tuple(r) for r in db.execute('SELECT rowid,id,time_ms,payload FROM ip_inputs ORDER BY time_ms,rowid')]
        self.assertEqual(shared['inputs'], actual)

    def test_restart_preserves_order_duplicate_and_immutability(self):
        path, before = self.replay('L2', True)
        store = SingleWriterStore(path)
        try:
            om = UnifiedPaperManager(store, start_ms=AT)
            session = IndexedHistoricalSession(om, 'L2')
            session.on_event(events()[0])
            self.assertEqual(store.db.execute('SELECT count(*) FROM ip_inputs').fetchone()[0], len(before['inputs']))
            for sql in ('DELETE FROM ip_inputs', 'DELETE FROM ip_input_refs', 'DELETE FROM ip_input_storage'):
                with self.assertRaises(sqlite3.IntegrityError): store.db.execute(sql)
            self.assertEqual(before['account'], om.account())
        finally: store.close()

    def test_missing_pool_is_not_recreated(self):
        path, _ = self.replay('L2', True)
        self.pool.rename(self.pool.with_suffix('.saved'))
        with self.assertRaises(FileNotFoundError): read_connection(path)
        with self.assertRaises(FileNotFoundError): SingleWriterStore(path)
        self.assertFalse(self.pool.exists())

    def test_rollback_has_no_dangling_reference_and_variant_is_distinct(self):
        store = SingleWriterStore(self.root/'L2/paper.sqlite', shared_inputs=self.pool)
        try:
            with self.assertRaises(RuntimeError):
                with store.db:
                    store.db.execute('INSERT INTO ip_inputs(id,time_ms,payload) VALUES(?,?,?)', ('one', 1, '{"raw":1}'))
                    raise RuntimeError('injected rollback')
            self.assertEqual(store.db.execute('SELECT count(*) FROM ip_inputs').fetchone()[0], 0)
            with store.db:
                store.db.execute('INSERT INTO ip_inputs(id,time_ms,payload) VALUES(?,?,?)', ('two', 1, '{"raw":2}'))
            self.assertEqual(store.db.execute('SELECT payload FROM ip_inputs').fetchone()[0], '{"raw":2}')
        finally: store.close()

    def test_existing_collector_history_not_migrated(self):
        path, _ = self.replay('legacy', False)
        with self.assertRaises(ValueError): SingleWriterStore(path, shared_inputs=self.pool)
        self.assertFalse(self.pool.exists())

    def test_parallel_models_same_millisecond_order_and_distinct_payloads(self):
        barrier = threading.Barrier(3)
        def lane(n):
            barrier.wait(timeout=10)
            store = SingleWriterStore(self.root/str(n)/'paper.sqlite', shared_inputs=self.pool)
            try:
                with store.db:
                    for i in range(50):
                        store.db.execute('INSERT INTO ip_inputs(id,time_ms,payload) VALUES(?,?,?)',
                                         (str(i), 1, '{"observation":%d}' % i))
                return [r[0] for r in store.db.execute('SELECT id FROM ip_inputs ORDER BY time_ms,rowid')]
            finally: store.close()
        with ThreadPoolExecutor(max_workers=3) as executor:
            results = list(executor.map(lane, range(3)))
        self.assertTrue(all(r == [str(i) for i in range(50)] for r in results))
        with closing(sqlite3.connect(self.pool)) as pool:
            self.assertEqual(pool.execute('SELECT count(*) FROM inputs').fetchone()[0], 50)

    def test_shared_input_cannot_use_future_frame(self):
        store = SingleWriterStore(self.root/'future/paper.sqlite', shared_inputs=self.pool)
        try:
            session = IndexedHistoricalSession(UnifiedPaperManager(store, start_ms=AT), 'L2')
            event = events()[0]
            event['frames'][0]['received_ms'] = AT + 1000
            with self.assertRaises(ValueError): session.on_event(event)
            self.assertEqual(store.db.execute('SELECT count(*) FROM ip_inputs').fetchone()[0], 0)
        finally: store.close()


if __name__ == '__main__': unittest.main()
