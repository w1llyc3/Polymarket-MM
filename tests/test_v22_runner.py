import json
import tempfile
import unittest
import time
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from nba_recorder.inplay.demo import AT, frame
from nba_recorder.inplay.manager import UnifiedPaperManager
from nba_recorder.inplay.runner import PaperRunner
from nba_recorder.inplay.store import SingleWriterStore
from nba_recorder.fill_management.ledger import balance
from nba_recorder.inplay.report import execution_evidence


class RunnerBoundaryTests(unittest.TestCase):
    def test_disconnect_queued_trade_before_watchdog_blocks_new_submission_keeps_fill(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SingleWriterStore(Path(tmp)/"paper.sqlite")
            try:
                om = UnifiedPaperManager(store, start_ms=AT)
                runner = PaperRunner(om, "L1")
                runner.session.observe(frame(AT))
                a = runner.session.observe(frame(AT+1))
                b = runner.session.observe(frame(AT+2, "b"))
                runner.session.quote(a, "a-only")
                om.advance(AT+7)
                original_submits = store.db.execute("SELECT count(*) FROM pe_journal WHERE kind='SUBMIT_REQUEST'").fetchone()[0]
                self.assertEqual(original_submits, 1)
                runner.latest = {"a": a, "b": b}
                runner.connection_guard = lambda: False
                trade = {"event_id": "queued-before-disconnect", "asset_id": "a", "condition_id": "synthetic-condition", "side": "SELL", "price": ".47", "size": "2", "exchange_ms": AT+8}
                with patch("nba_recorder.inplay.runner.utc_ms", return_value=AT+10):
                    runner.feed(list(runner.latest.values()), [trade], reuse=True)
                self.assertEqual(runner.ticks, 0)
                cooked = json.loads(store.db.execute("SELECT payload FROM ip_inputs ORDER BY rowid DESC LIMIT 1").fetchone()[0])
                self.assertTrue(all(f["connected"] is False for f in cooked["frames"]))
                om.advance(AT+12)
                self.assertEqual(balance(om.ledger.state(), "a"), 2)
                self.assertEqual(store.db.execute("SELECT count(*) FROM pe_journal WHERE kind='SUBMIT_REQUEST'").fetchone()[0], original_submits)
                self.assertTrue(om.halted)
                self.assertTrue(om.consistent())
            finally:
                store.close()

    def test_reconnect_epoch_and_failed_watchdog_cannot_authorize_old_frames(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SingleWriterStore(Path(tmp)/"paper.sqlite")
            try:
                om = UnifiedPaperManager(store, start_ms=AT)
                runner = PaperRunner(om)
                runner.connection_guard = lambda: True
                runner.connection_epoch = lambda: 2
                old = {**frame(AT), "connection_epoch": 1}
                self.assertFalse(runner.authorize(old)["connected"])
                fresh = {**old, "connection_epoch": 2}
                self.assertTrue(runner.authorize(fresh)["connected"])
                self.assertFalse(runner.authorize({**fresh, "message_epoch": 1})["connected"])
                runner.watchdog_task = SimpleNamespace(done=lambda: True)
                self.assertFalse(runner.authorize(fresh)["connected"])
                runner.watchdog_task = SimpleNamespace(done=lambda: False)
                runner.last_watchdog_tick_mono = time.monotonic()-2
                self.assertFalse(runner.authorize(fresh)["connected"])
                runner.last_watchdog_tick_mono = time.monotonic()
                self.assertTrue(runner.authorize(fresh)["connected"])
                self.assertTrue(old["connected"])
            finally:
                store.close()

    def test_guard_rechecked_at_dispatch_even_when_no_new_feed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SingleWriterStore(Path(tmp)/"paper.sqlite")
            try:
                om = UnifiedPaperManager(store, start_ms=AT)
                runner = PaperRunner(om, "L1")
                runner.session.observe(frame(AT))
                f = runner.session.observe(frame(AT+1))
                from nba_recorder.inplay.reference import book_targets
                target = book_targets(f, om.account(), [], {}, om.policy)["targets"][0]
                key = om.enqueue("queued", target, f)
                runner.connection_guard = lambda: False
                self.assertEqual(om.dispatch(), 0)
                self.assertEqual(om.order(key)["state"], "REJECTED")
                evidence = execution_evidence(om)
                self.assertEqual(evidence["intents_created"], 1)
                self.assertEqual(evidence["local_rejections"], 1)
                self.assertEqual(evidence["paper_orders_accepted"], 0)
                self.assertEqual(evidence["quote_execution_acceptance_status"], "NOT_VERIFIED")
            finally:
                store.close()

    def test_backlogged_input_no_quote_but_preserves_raw_connectivity(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SingleWriterStore(Path(tmp)/"paper.sqlite")
            try:
                om = UnifiedPaperManager(store, start_ms=AT)
                runner = PaperRunner(om)
                runner.connection_guard = lambda: True
                with patch("nba_recorder.inplay.runner.utc_ms", return_value=AT+600):
                    runner.feed([frame(AT)])
                self.assertFalse(om.orders())
                raw = frame(AT)
                self.assertTrue(raw["connected"])
                self.assertEqual(runner.latest["a"]["backlog_ms"], 600)
            finally:
                store.close()
