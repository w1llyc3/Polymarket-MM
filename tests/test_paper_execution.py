import copy
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from test_quote_engine import AT, frame, event, trade
from nba_recorder.paper_execution.store import ExecutionStore
from nba_recorder.paper_execution.manager import OrderManager
from nba_recorder.paper_execution.risk import ExecutionPolicy, commitments
from nba_recorder.paper_execution.hedge import evaluate_hedge
from nba_recorder.paper_execution.session import PaperSession
from nba_recorder.quote_engine.storage import PaperStore


class PaperExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/"execution.sqlite"
        self.store = ExecutionStore(self.path)
        self.policy = ExecutionPolicy(initial_inventory="10", target_inventory="10")
        self.om = OrderManager(self.store, self.policy, AT)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def queued(self, request="one", size="5", asset="a", side="BUY", price=".49"):
        f = frame(self.om.now)
        f["asset_id"] = f["book"]["asset_id"] = f["signal"]["asset_id"] = asset
        target = {"side": side, "price": price, "size": size, "expires_ms": self.om.now+30000, "maker_only": True}
        return self.om.enqueue(request, target, f)

    def opened(self, **kwargs):
        order = self.queued(**kwargs)
        self.om.dispatch()
        self.om.advance(self.om.now+5)
        self.assertEqual(self.om.order(order)["state"], "OPEN")
        return order

    def test_full_fill_before_cancel(self):
        order = self.opened()
        self.om.fill(order, "5", "full")
        self.om.advance(self.om.now)
        self.om.cancel(order)
        self.assertEqual(self.om.order(order)["state"], "FILLED")
        self.assertEqual(Decimal(self.om.account()["positions"]["a"]), 15)
        self.assertTrue(self.om.consistent())

    def test_partial_fill_during_cancel(self):
        order = self.opened()
        self.om.cancel(order)
        self.om.fill(order, "2", "partial")
        self.om.advance(self.om.now)
        self.assertEqual(self.om.order(order)["state"], "CANCEL_PENDING")
        self.assertEqual(Decimal(self.om.account()["positions"]["a"]), 12)
        budget = commitments(self.om.account(), self.om.orders())
        self.assertEqual(budget["account_units"], 15)
        self.om.advance(self.om.now+10)
        self.assertEqual(self.om.order(order)["state"], "CANCELLED")
        self.assertEqual(commitments(self.om.account(), self.om.orders())["account_units"], 12)
        self.assertTrue(self.om.consistent())

    def test_cancel_timeout_not_success_and_unknown_never_resumes(self):
        order = self.opened()
        self.om.cancel(order, drop_response=True)
        self.om.advance(self.om.now+100)
        self.assertEqual(self.om.order(order)["state"], "UNKNOWN")
        self.assertTrue(self.om.halted)
        self.assertFalse(self.om.resume())
        self.assertTrue(self.om.reconcile())
        self.assertEqual(self.om.order(order)["state"], "RECONCILED")
        self.assertTrue(self.om.halted)
        self.assertTrue(self.om.resume())
        self.assertEqual(self.om.order(order)["state"], "CANCELLED")

    def test_submit_timeout_but_exchange_accepted(self):
        order = self.queued()
        self.om.dispatch(drop_response=True)
        self.assertEqual(self.om.adapter.authoritative(order)["state"], "OPEN")
        self.om.advance(AT+100)
        self.assertEqual(self.om.order(order)["state"], "UNKNOWN")
        self.om.fill(order, "2", "late-execution")
        self.om.advance(self.om.now)
        self.assertEqual(self.om.order(order)["state"], "RECONCILED")
        self.assertEqual(Decimal(self.om.account()["positions"]["a"]), 12)
        self.assertTrue(self.om.halted)
        self.om.advance(self.om.now+10)
        self.assertTrue(self.om.reconcile())
        self.assertTrue(self.om.halted)

    def test_kill_atomically_clears_queue_blocks_new_orders(self):
        live = self.opened()
        queued = self.queued("queued")
        self.om.kill("operator_test")
        self.assertEqual(self.om.order(queued)["state"], "CANCELLED")
        self.assertEqual(self.om.order(live)["state"], "CANCEL_PENDING")
        before = len(self.om.account()["positions"])
        blocked = self.queued("blocked", asset="new-asset")
        self.assertEqual(self.om.order(blocked)["state"], "REJECTED")
        self.assertEqual(len(self.om.account()["positions"]), before)
        self.assertEqual(self.om.dispatch(), 0)
        self.om.advance(self.om.now+10)
        self.assertTrue(self.om.consistent())
        self.assertTrue(self.om.halted)

    def test_duplicate_fill_and_duplicate_report(self):
        order = self.opened()
        self.assertTrue(self.om.fill(order, "2", "same"))
        self.assertFalse(self.om.fill(order, "2", "same"))
        row = self.store.db.execute("SELECT * FROM pe_pending WHERE kind='REPORT' AND done=0").fetchone()
        with self.store.db:
            self.om.adapter.schedule(self.om.now, "REPORT", order, json.loads(row["payload"]))
        self.om.advance(self.om.now)
        self.assertEqual(Decimal(self.om.account()["positions"]["a"]), 12)
        self.assertTrue(self.om.consistent())

    def test_restart_recovers_intent_authority_and_stays_halted(self):
        order = self.opened()
        self.om.fill(order, "2", "unreceived", transport_ms=30)
        self.store.close()
        self.store = ExecutionStore(self.path)
        self.om = OrderManager(self.store, self.policy)
        self.assertTrue(self.om.halted)
        self.assertEqual(self.om.order(order)["state"], "UNKNOWN")
        self.om.advance(self.om.now+40)
        self.assertTrue(self.om.reconcile())
        self.assertEqual(Decimal(self.om.account()["positions"]["a"]), 12)
        self.assertTrue(self.om.halted)
        self.assertTrue(self.om.resume())

    def test_rest_delay_timeout_and_recovery_latency(self):
        order = self.opened()
        self.om.cancel(order, drop_response=True)
        self.om.advance(self.om.now+100)
        self.assertFalse(self.om.reconcile(latency_ms=101))
        self.assertTrue(self.om.halted)
        self.assertTrue(self.om.reconcile(latency_ms=2))
        stats = self.om.status()["latency_ms"]
        self.assertEqual(stats["authority_query"]["P95"], 100)
        self.assertIn("state_recovery", stats)

    def test_stale_odds_book_disconnect_edge_jump_start_and_market(self):
        mutations = [lambda f: f.update(connected=False), lambda f: f["signal"]["sources"][0].update(updated_ms=AT-200000), lambda f: f.update(book_received_ms=AT-40000), lambda f: f["signal"].update(fair_probability=".49"), lambda f: f["market"].update(game_start_ms=AT+300000), lambda f: f["market"]["status"].update(closed=True)]
        for index, mutation in enumerate(mutations):
            other = ExecutionStore(Path(self.tmp.name)/f"risk{index}.sqlite")
            try:
                om = OrderManager(other, self.policy, AT)
                target = {"side": "BUY", "price": ".49", "size": "5", "expires_ms": AT+30000}
                om.enqueue("risk", target, frame())
                om.dispatch()
                om.advance(AT+5)
                f = frame(AT+5)
                mutation(f)
                self.assertTrue(om.risk_check(f))
                self.assertTrue(om.halted)
                self.assertTrue(other.db.execute("SELECT 1 FROM pe_risks WHERE order_id IS NOT NULL").fetchone())
            finally:
                other.close()
        self.opened()
        self.assertIn("abrupt_price_change", self.om.risk_check(frame(self.om.now), previous_mid=".7"))

    def test_same_event_multiple_fills_preserve_committed_risk(self):
        a = self.queued("a", asset="a")
        b = self.queued("b", asset="b")
        self.om.dispatch()
        self.om.advance(AT+5)
        before = commitments(self.om.account(), self.om.orders())["event_units"]["synthetic-event"]
        self.om.fill(a, "3", "fill-a")
        self.om.fill(b, "4", "fill-b")
        self.om.advance(self.om.now)
        self.assertEqual(commitments(self.om.account(), self.om.orders())["event_units"]["synthetic-event"], before)
        self.assertTrue(self.om.consistent())

    def test_daily_loss_breaker_is_paper_and_latched(self):
        self.queued()
        self.assertEqual(self.om.mark_equity([frame()]), "0.00")
        self.om.policy = ExecutionPolicy(initial_inventory="10", max_daily_loss=".5")
        down = frame()
        down["book"].update(bid=".39", ask=".41")
        self.assertEqual(Decimal(self.om.mark_equity([down])), -1)
        self.assertTrue(self.om.halted)
        self.assertFalse(self.om.resume())

    def test_exchange_rejection_and_zero_network(self):
        order = self.queued()
        with patch("socket.socket", side_effect=AssertionError("network forbidden")), patch("httpx.Client", side_effect=AssertionError("network forbidden")):
            self.om.dispatch(accepted=False)
            self.om.advance(AT+5)
        self.assertEqual(self.om.order(order)["state"], "REJECTED")
        self.assertEqual(self.om.status()["real_submission_calls"], 0)

    def test_sell_reservation_and_cash_and_order_risk(self):
        first = self.queued("sell1", side="SELL", price=".51", size="10")
        second = self.queued("sell2", side="SELL", price=".51", size="5")
        self.assertEqual(self.om.order(first)["state"], "QUEUED")
        self.assertEqual(self.om.order(second)["state"], "REJECTED")
        huge = self.queued("huge", size="100")
        self.assertEqual(self.om.order(huge)["state"], "REJECTED")

    def test_delayed_submit_ack_cannot_regress_partial_fill(self):
        order = self.queued()
        self.om.dispatch(response_ms=40)
        self.om.fill(order, "2", "early-fill")
        self.om.advance(AT)
        self.om.advance(AT+40)
        self.assertEqual(self.om.order(order)["state"], "PARTIALLY_FILLED")
        self.assertEqual(Decimal(self.om.order(order)["filled"]), 2)

    def test_expiry_cancels_and_journal_restores_cache(self):
        order = self.opened()
        self.om.advance(AT+30020)
        self.assertEqual(self.om.order(order)["state"], "CANCELLED")
        with self.store.db:
            self.store.db.execute("UPDATE pe_control SET account='{}'")
        self.store.close()
        self.store = ExecutionStore(self.path)
        self.om = OrderManager(self.store, self.policy)
        self.assertIn("cash", self.om.account())
        self.assertTrue(self.om.halted)

    def test_quote_session_replay_trade_proxy_and_duplicate_cursor(self):
        tape_store = PaperStore(Path(self.tmp.name)/"tape.sqlite")
        try:
            events = [event(frame()), event(frame(AT+1000), [trade(AT+1000)]), event(frame(AT+2000))]
            tape = tape_store.seal(events, {"start_ms": AT, "end_ms": AT+2000})
            session = PaperSession(self.om)
            session.replay(tape_store, tape)
            self.assertEqual(Decimal(self.om.account()["positions"]["a"]), 12)
            count = self.store.db.execute("SELECT count(*) FROM pe_fills").fetchone()[0]
            session.replay(tape_store, tape)
            self.assertEqual(self.store.db.execute("SELECT count(*) FROM pe_fills").fetchone()[0], count)
            self.assertEqual(count, 1)
            self.assertEqual(self.om.status()["real_submission_calls"], 0)
        finally:
            tape_store.close()

    def test_paper_state_machine_rejects_impossible_transition(self):
        order = self.opened()
        self.om.fill(order, "5", "terminal")
        self.om.advance(self.om.now)
        invalid = self.om.order(order)
        invalid["state"] = "OPEN"
        with self.store.db, self.assertRaises(ValueError):
            self.om._save(invalid, "invalid")

    def test_risk_detection_fill_race_retains_inventory(self):
        order = self.opened()
        self.om.fill(order, "2", "in-flight", transport_ms=1)
        f = frame(self.om.now)
        f["connected"] = False
        self.om.risk_check(f)
        self.assertEqual(Decimal(self.om.account()["positions"]["a"]), 12)
        self.assertTrue(self.om.halted)
        self.om.advance(self.om.now+10)
        self.assertTrue(self.om.consistent())

    def test_reconcile_mismatch_keeps_halted(self):
        order = self.opened()
        self.om.kill("stop")
        self.om.advance(self.om.now+10)
        with self.store.db:
            authority = self.om.adapter.authoritative(order)
            authority["asset_id"] = "wrong-asset"
            self.store.db.execute("UPDATE pe_exchange SET payload=? WHERE id=?", (json.dumps(authority), order))
        # Identity mismatch is retained as an unresolved risk, not accepted as normal trading.
        self.om.reconcile()
        self.assertTrue(self.om.halted)
        self.assertFalse(self.om.resume())


class HedgeTests(unittest.TestCase):
    def test_depth_comparison_and_stale_rejection(self):
        f = frame()
        m = {**f["market"], "outcomes": [{"asset_id": "a"}, {"asset_id": "b"}]}
        books = {}
        for asset in ("a", "b"):
            books[asset] = {**copy.deepcopy(f["book"]), "asset_id": asset, "connected": True, "local_receive_timestamp": AT}
        result = evaluate_hedge(m, "a", "5", {"a": "10"}, books, AT, fair_probability=".5")
        self.assertEqual(result["status"], "RESEARCH")
        self.assertEqual(Decimal(result["plans"][0]["net_future_value"]), Decimal("2.45"))
        self.assertEqual(Decimal(result["plans"][1]["conditional_paired_future_value"]), Decimal("2.45"))
        self.assertEqual(evaluate_hedge(m, "a", "5", {"a": "10"}, books, AT+30001)["status"], "NO_RESEARCH")
        self.assertFalse(result["auto_execute"])
