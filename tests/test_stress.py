import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from nba_recorder.stress.engine import CASES, FaultInjectionEngine
from nba_recorder.stress.engine import AT, fixture
from nba_recorder.paper_execution.manager import OrderManager
from nba_recorder.paper_execution.store import ExecutionStore
from nba_recorder.paper_execution.risk import ExecutionPolicy


class StressTests(unittest.TestCase):
    def test_deterministic_complete_fault_matrix_and_zero_network(self):
        with tempfile.TemporaryDirectory() as tmp, patch("socket.socket", side_effect=AssertionError("no network")), patch("httpx.Client.request", side_effect=AssertionError("no HTTP")):
            a = FaultInjectionEngine(Path(tmp)/"a").run()
            b = FaultInjectionEngine(Path(tmp)/"b").run()
            self.assertEqual(a, b)
            self.assertEqual(a["failed"], 0, a)
            self.assertEqual(a["passed"], len(CASES))

    def test_no_overwrite_and_unknown_fault_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = FaultInjectionEngine(tmp)
            engine.run_case("pause")
            with self.assertRaises(ValueError):
                engine.run_case("pause")
            with self.assertRaises(ValueError):
                engine.run_case("not-a-case")

    def test_submission_uses_latest_persisted_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ExecutionStore(Path(tmp)/"state.sqlite")
            try:
                om = OrderManager(store, ExecutionPolicy(), AT)
                key = om.enqueue("one", {"side": "BUY", "price": ".49", "size": "5", "expires_ms": AT+30000}, fixture())
                # Simulate a source update persisted before dispatch, without a
                # completed risk-detection cycle. Submission must recheck it.
                import json
                current = fixture()
                current["signal"]["fair_probability"] = ".4"
                with store.db:
                    store.db.execute("INSERT INTO pe_risk_frames VALUES(?,?,?)", ("a", AT, json.dumps(current)))
                self.assertEqual(om.dispatch(), 0)
                self.assertEqual(om.order(key)["state"], "REJECTED")
                self.assertIsNone(om.adapter.authoritative(key))
            finally:
                store.close()

    def test_reconciling_blocks_submission_and_orphan_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ExecutionStore(Path(tmp)/"state.sqlite")
            try:
                om = OrderManager(store, start_ms=AT)
                original = om.advance
                def during_barrier(at):
                    self.assertEqual(om.risk_state, "RECONCILING")
                    self.assertEqual(om.dispatch(), 0)
                    original(at)
                with patch.object(om, "advance", side_effect=during_barrier):
                    self.assertTrue(om.reconcile())
                self.assertEqual(om.risk_state, "READY_FOR_MANUAL_RESET")
                with store.db:
                    store.db.execute("INSERT INTO pe_exchange VALUES('orphan','{}')")
                self.assertFalse(om.reconcile())
                self.assertEqual(om.risk_state, "HALTED")
                self.assertFalse(om.resume())
            finally:
                store.close()


def scenario_test(name):
    def test(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = FaultInjectionEngine(tmp).run_case(name)
            self.assertEqual(result["status"], "PASS")
            self.assertEqual(result["real_submission_calls"], 0)
    return test


for scenario in CASES:
    setattr(StressTests, "test_fault_"+scenario, scenario_test(scenario))
