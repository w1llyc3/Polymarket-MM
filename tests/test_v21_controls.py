import asyncio
import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from nba_recorder.api import utc_ms
from nba_recorder.inplay.demo import AT, frame
from nba_recorder.inplay.manager import UnifiedPaperManager
from nba_recorder.inplay.policy import BookPolicy
from nba_recorder.inplay.reference import book_targets, reduction_capacity
from nba_recorder.inplay.session import BookSession
from nba_recorder.inplay.store import SingleWriterStore
from nba_recorder.inplay.runner import PaperRunner
from nba_recorder.fill_management.ledger import balance


class ControlRegressions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SingleWriterStore(Path(self.tmp.name)/"paper.sqlite")
        self.om = UnifiedPaperManager(self.store, start_ms=AT)
        self.session = BookSession(self.om, "L1")
        self.session.observe(frame(AT))
        self.f = self.session.observe(frame(AT+1))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_twenty_stable_frames_preserve_order_ids_and_partial_remainders(self):
        self.om.split(self.f["market"], "5", "split")
        self.session.quote(self.f, "start")
        self.om.advance(self.om.now+5)
        original = {o["id"] for o in self.om.orders()}
        sell = next(o for o in self.om.orders() if o["side"] == "SELL")
        self.om.fill(sell["id"], "2", "partial")
        self.om.advance(self.om.now)
        for i in range(20):
            current = self.session.observe(frame(self.om.now+10))
            decision = self.session.quote(current, "stable-"+str(i))
            self.assertTrue(all(t["action"] == "RETAIN" for t in decision["targets"]))
            self.assertEqual({o["id"] for o in self.om.orders()}, original)
            self.assertTrue(all(o["state"] in {"OPEN", "PARTIALLY_FILLED"} for o in self.om.orders()))
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM pe_journal WHERE kind='CANCEL_REQUEST'").fetchone()[0], 0)
        self.assertTrue(self.om.consistent())

    def test_near_cash_and_inventory_cap_buy_retains_itself(self):
        other = SingleWriterStore(Path(self.tmp.name)/"near.sqlite")
        try:
            om = UnifiedPaperManager(other, BookPolicy(initial_cash="2.4", max_asset_inventory="5"), AT)
            session = BookSession(om, "L1")
            session.observe(frame(AT))
            fresh = session.observe(frame(AT+1))
            session.quote(fresh, "q")
            om.advance(AT+6)
            first = om.orders()[0]["id"]
            for i in range(20):
                session.quote(session.observe(frame(om.now+10)), "again"+str(i))
            self.assertEqual(len(om.orders()), 1)
            self.assertEqual(om.order(first)["state"], "OPEN")
        finally:
            other.close()

    def test_changed_price_waits_for_cancel_ack_before_replacement(self):
        self.om.split(self.f["market"], "5", "split")
        self.session.quote(self.f, "q")
        self.om.advance(AT+6)
        originals = {o["id"] for o in self.om.orders()}
        f = frame(AT+7)
        f["book"].update(bid=".50", ask=".52", bids=[{"price": ".50", "size": "100"}], asks=[{"price": ".52", "size": "100"}])
        current = self.session.observe(f)
        self.session.quote(current, "changed")
        self.assertTrue(all(o["state"] == "CANCEL_PENDING" for o in self.om.orders()))
        self.session.quote(current, "pending-again")
        self.assertEqual({o["id"] for o in self.om.orders()}, originals)
        self.om.advance(AT+17)
        self.session.quote(self.session.observe({**f, "time_ms": AT+18}), "replacement")
        self.assertGreater(len(self.om.orders()), 2)
        self.assertTrue(self.om.consistent())

    def test_public_sixteen_shares_without_own_fill_is_diagnostic_not_halted(self):
        trade = {"event_id": "sixteen", "asset_id": "a", "condition_id": "synthetic-condition", "side": "SELL", "price": ".48", "size": "16", "exchange_ms": AT+1}
        self.session.executions([self.f], [trade], "diag")
        self.assertEqual(self.om.risk_state, "ACTIVE")
        self.assertEqual(balance(self.om.ledger.state(), "a"), 0)
        row = json.loads(self.store.db.execute("SELECT payload FROM ip_flow_diagnostics").fetchone()[0])
        self.assertEqual(row["risk_level"], "DIAGNOSTIC")
        self.assertEqual(row["own_fill_quantity"], "0")

    def test_paired_vs_directional_inventory_named_one_sided_rule(self):
        paired = {"cash": "90", "positions": {"a": "10", "b": "10"}, "events": {"a": "synthetic-event", "b": "synthetic-event"}}
        directional = {"cash": "90", "positions": {"a": "20", "b": "0"}, "events": paired["events"]}
        a = book_targets(self.f, paired, [], {}, self.om.policy)
        b = book_targets(self.f, directional, [], {}, self.om.policy)
        self.assertEqual(a["quote_center"], "0.500")
        self.assertLess(float(b["quote_center"]), float(a["quote_center"]))
        self.assertEqual(next(t["price"] for t in a["targets"] if t["side"] == "SELL"), next(t["price"] for t in b["targets"] if t["side"] == "SELL"))
        self.assertIn("SUPPRESS_BUY", a["inventory_rule"])

    def test_delayed_public_print_before_cancel_fills_causally_after_local_cancel(self):
        self.session.quote(self.f, "q")
        self.om.advance(AT+6)
        order = self.om.orders()[0]
        self.om.advance(AT+10)
        self.om.cancel(order["id"])
        self.om.advance(AT+20)
        self.assertEqual(self.om.order(order["id"])["state"], "CANCELLED")
        late = frame(AT+30, period=4, remaining=60)
        trade = {"event_id": "late-before-cancel", "asset_id": "a", "condition_id": "synthetic-condition", "side": "SELL", "price": ".47", "size": "2", "exchange_ms": AT+7, "received_ms": AT+9, "processed_ms": AT+30, "strategy_available_ms": AT+30}
        self.session.on_event({"id": "closed-before-print-available", "time_ms": AT+30, "frames": [late], "trades": [trade]})
        self.om.advance(AT+32)
        self.assertEqual(balance(self.om.ledger.state(), "a"), 2)
        self.assertTrue(self.om.halted)
        self.assertEqual(self.store.db.execute("SELECT time_ms FROM pe_fills WHERE event_id IS NOT NULL").fetchone()[0], AT+7)
        self.assertTrue(self.om.consistent())

    def test_no_message_watchdog_cancels_resting_orders(self):
        runner = PaperRunner(self.om, "L1")
        self.session.quote(self.f, "q")
        self.om.advance(AT+6)
        runner.latest = {"a": self.f}
        runner.tick(AT+4000)  # no source delivery between open and timeout
        self.om.advance(AT+4010)
        self.assertTrue(all(o["confirmed_state"] == "CANCELLED" for o in self.om.orders()))
        # Flat after TTL cancellation need not HALT; safety is no active risk.
        self.assertFalse(any(o["state"] in {"OPEN", "PARTIALLY_FILLED", "SUBMITTING"} for o in self.om.orders()))
        self.assertGreater(runner.ticks, 0)

    def test_manual_expiry_not_upstream_freshness_and_auto_mode_stays_unknown(self):
        with self.assertRaises(ValueError):
            BookPolicy(manual_max_age_ms=15001)
        other = SingleWriterStore(Path(self.tmp.name)/"manual.sqlite")
        try:
            policy = BookPolicy(allow_manual_phase=True)
            om = UnifiedPaperManager(other, policy, AT)
            session = BookSession(om, "L1")
            grant = {"authority_mode": "MANUAL_CONFIRMED", "confirmation_id": "human1", "market_id": "m", "event_id": "synthetic-event", "game_id": "g", "operator_id": "test operator", "evidence": "synthetic observed clock", "exit_instructions": "cancel at expiry, hold stopped for review", "confirmed_ms": AT, "expires_ms": AT+1000, "permission": "ACTIVE_MM", "period": 2, "remaining_s": 600, "clock_observation": "RUNNING", "status_observation": "IN_PROGRESS"}
            with other.db:
                other.db.execute("INSERT INTO ip_manual_confirmations VALUES(?,?,?)", ("human1", AT, json.dumps(grant)))
            raw = frame(AT)
            raw["sports"] = {"paused": None, "observed_ms": None}
            controlled = session.observe(raw)
            self.assertEqual(controlled["phase"], "ACTIVE_MM")
            self.assertIsNone(controlled["sports"]["paused"])
            self.assertTrue(session.quote(controlled, "manual-q")["targets"])
            runner = PaperRunner(om, "L1")
            runner.latest = {"a": raw}
            runner.tick(AT+1001)
            self.assertTrue(om.halted)
            self.assertEqual(om.phases.get("m")["state"], "UNKNOWN")
        finally:
            other.close()

    def test_same_ms_submit_and_cancel_prints_are_explicitly_ambiguous(self):
        self.session.quote(self.f, "q")
        order = self.om.orders()[0]
        self.om.advance(AT+6)
        trade = {"event_id": "same-submit", "asset_id": "a", "condition_id": "synthetic-condition", "side": "SELL", "price": ".47", "size": "2", "exchange_ms": order["submit_ms"]}
        self.session.executions([self.f], [trade], "same")
        self.assertEqual(balance(self.om.ledger.state(), "a"), 0)
        self.om.cancel(order["id"])
        self.om.advance(AT+16)
        cancelled = self.om.adapter.authoritative(order["id"])["cancelled_ms"]
        self.session.executions([self.f], [{**trade, "event_id": "same-cancel", "exchange_ms": cancelled}], "same-cancel")
        self.assertEqual(balance(self.om.ledger.state(), "a"), 0)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM pe_journal WHERE kind='SAME_MS_EXECUTION_AMBIGUOUS'").fetchone()[0], 2)

    def test_reduce_only_retains_twenty_frames_without_extra_capacity(self):
        self.om.split(self.f["market"], "5", "split")
        self.session.quote(self.f, "buy")
        self.om.advance(AT+6)
        buy = next(o for o in self.om.orders() if o["side"] == "BUY")
        self.om.fill(buy["id"], "5", "fill")
        self.om.advance(AT+8)
        for o in self.om.orders():
            if o["state"] == "OPEN":
                self.om.cancel(o["id"])
        self.om.advance(AT+18)
        reduced = self.session.observe(frame(AT+19, period=4, remaining=120))
        self.session.quote(reduced, "reduce")
        self.om.advance(AT+24)
        exit_order = next(o for o in self.om.orders() if o["state"] == "OPEN")
        for i in range(20):
            fresh = self.session.observe(frame(self.om.now+10, period=4, remaining=120))
            decision = self.session.quote(fresh, "reduce"+str(i))
            self.assertEqual(decision["targets"][0]["retained_order_id"], exit_order["id"])
            self.assertEqual(self.om.order(exit_order["id"])["state"], "OPEN")
        self.assertTrue(self.om.consistent())

    def test_complementary_pending_reduction_keeps_shared_capacity_reserved(self):
        account = {"cash": "90", "positions": {"a": "10", "b": "5"}}
        sell_a = {"asset_id": "a", "side": "SELL"}
        buy_b = {"asset_id": "b", "side": "BUY", "size": "5", "filled": "0"}
        self.assertEqual(reduction_capacity(sell_a, self.f, account, []), 5)
        for state in ("CANCEL_PENDING", "UNKNOWN", "SUBMITTING"):
            self.assertEqual(reduction_capacity(sell_a, self.f, account, [{**buy_b, "state": state}]), 0)

    def test_unknown_sports_timestamp_at_submission_boundary_is_unknown(self):
        raw = copy.deepcopy(self.f)
        raw["sports"]["observed_ms"] = None
        self.assertEqual(self.om.phases.boundary(raw, self.om.now), "UNKNOWN")

    def test_incompatible_restart_does_not_write_new_capital_or_replace_ledger(self):
        saved = json.loads(self.store.db.execute("SELECT config FROM pe_control WHERE id=1").fetchone()[0])
        saved.pop("allow_manual_phase")
        with self.store.db:
            self.store.db.execute("UPDATE pe_control SET config=? WHERE id=1", (json.dumps(saved),))
        before = self.store.db.execute("SELECT count(*) FROM l6_events").fetchone()[0]
        with self.assertRaisesRegex(ValueError, "fresh v2.1"):
            UnifiedPaperManager(self.store, self.om.policy)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM l6_events").fetchone()[0], before)


class IndependentWatchdog(unittest.IsolatedAsyncioTestCase):
    async def test_watchdog_runs_while_source_is_permanently_waiting(self):
        with tempfile.TemporaryDirectory() as path:
            store = SingleWriterStore(Path(path)/"silent.sqlite")
            try:
                at = utc_ms()
                om = UnifiedPaperManager(store, start_ms=at)
                runner = PaperRunner(om, "L1", watchdog_ms=10)
                runner.session.observe(frame(at))
                f = runner.session.observe(frame(at+1))
                runner.session.quote(f, "q")
                om.advance(at+6)
                runner.latest = {"a": f}
                # Transport loss is independent of a new market callback.
                runner.connection_guard = lambda: False
                blocked_source = asyncio.create_task(asyncio.Event().wait())
                watchdog = asyncio.create_task(runner.watchdog())
                await asyncio.sleep(.08)
                runner.stopping.set()
                await watchdog
                blocked_source.cancel()
                await asyncio.gather(blocked_source, return_exceptions=True)
                self.assertGreater(runner.ticks, 1)
                self.assertTrue(om.halted)
                self.assertTrue(all(o["confirmed_state"] == "CANCELLED" for o in om.orders()))
            finally:
                store.close()
