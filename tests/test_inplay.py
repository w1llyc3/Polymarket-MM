import copy
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from nba_recorder.inplay.demo import AT, frame
from nba_recorder.inplay.policy import BookPolicy
from nba_recorder.inplay.manager import UnifiedPaperManager
from nba_recorder.inplay.session import BookSession
from nba_recorder.inplay.store import SingleWriterStore
from nba_recorder.inplay.reference import book_targets, submission_risk
from nba_recorder.quote_engine.model import Policy, targets
from nba_recorder.fill_management.ledger import balance


class InplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/"paper.sqlite"
        self.store = SingleWriterStore(self.path)
        self.policy = BookPolicy()
        self.om = UnifiedPaperManager(self.store, self.policy, AT)
        self.session = BookSession(self.om, "L1")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def synced(self, at=AT):
        self.session.observe(frame(at))
        return self.session.observe(frame(at+1))

    def opened(self):
        f = self.synced()
        decision = self.session.quote(f, "decision")
        self.om.advance(self.om.now+5)
        order = next(o for o in self.om.orders() if o["side"] == "BUY")
        self.assertEqual(order["state"], "OPEN")
        return order, f

    def test_book_driven_without_odds_external_fair_still_refuses(self):
        f = self.synced()
        decision = self.session.quote(f, "q")
        self.assertEqual(decision["state"], "BUY_ONLY")
        self.assertNotIn("fair_probability", decision)
        self.assertIn("reference_mid", decision)
        self.assertNotIn("gross_edge", decision["targets"][0])
        external = targets(f, self.om.account(), [], {"a": {"event_id": "synthetic-event"}}, ".01", Policy())
        self.assertEqual(external["state"], "NO_QUOTE")
        self.assertFalse(any(decimal_q != "0" for decimal_q in self.om.account()["positions"].values()))

    def test_bad_books_all_modes_reject(self):
        f = self.synced()
        mutations = [lambda v: v.update(connected=False), lambda v: v.update(book_received_ms=AT-10000), lambda v: v["book"].update(bid=".6"), lambda v: v["book"].update(asset_id="foreign"), lambda v: v.update(backlog_ms=99999)]
        for change in mutations:
            bad = copy.deepcopy(f)
            change(bad)
            self.assertEqual(book_targets(bad, self.om.account(), [], {}, self.policy)["state"], "NO_QUOTE")
            self.assertEqual(targets(bad, self.om.account(), [], {}, ".01", Policy())["state"], "NO_QUOTE")

    def test_unknown_phase_and_unknown_elapsed_reject(self):
        f = frame()
        f["sports"]["clock_semantics"] = "UNVERIFIED_ELAPSED"
        f = self.session.observe(f)
        self.assertEqual(f["phase"], "UNKNOWN")
        self.assertEqual(self.session.quote(f, "unknown")["state"], "NO_QUOTE")

    def test_close_race_dispatch_and_restart_overtime_latch(self):
        f = self.synced()
        target = book_targets(f, self.om.account(), [], {}, self.policy)["targets"][0]
        order_id = self.om.enqueue("before-close", target, f)
        late = self.session.observe(frame(self.om.now+1, period=4, remaining=60))
        self.assertEqual(late["phase"], "MM_CLOSED")
        self.assertEqual(self.om.dispatch(), 0)
        self.assertIsNone(self.om.adapter.authoritative(order_id))
        self.store.close()
        self.store = SingleWriterStore(self.path)
        self.om = UnifiedPaperManager(self.store, self.policy)
        self.session = BookSession(self.om, "L1")
        overtime = self.session.observe(frame(self.om.now+1, period=5, remaining=300))
        self.assertEqual(overtime["phase"], "MM_CLOSED")
        self.assertTrue(self.om.halted)

    def test_cancel_inflight_fill_same_ledger_and_restart(self):
        order, f = self.opened()
        self.om.cancel(order["id"])
        self.om.fill(order["id"], "2", "race")
        self.om.advance(self.om.now)
        self.assertEqual(self.om.order(order["id"])["state"], "CANCEL_PENDING")
        self.assertEqual(balance(self.om.ledger.state(), "a"), 2)
        self.assertTrue(self.om.consistent())
        self.om.advance(self.om.now+10)
        state = self.om.ledger.state()
        self.store.close()
        self.store = SingleWriterStore(self.path)
        self.om = UnifiedPaperManager(self.store, self.policy)
        self.assertEqual(self.om.ledger.state(), state)
        self.assertTrue(self.om.reconcile())
        self.assertTrue(self.om.consistent())
        self.assertTrue(self.om.halted)

    def test_duplicate_fill_does_not_duplicate_fifo(self):
        order, _ = self.opened()
        self.om.fill(order["id"], "2", "one")
        self.om.advance(self.om.now)
        before = self.om.ledger.state()
        self.assertFalse(self.om.fill(order["id"], "2", "one"))
        self.om.advance(self.om.now)
        self.assertEqual(before, self.om.ledger.state())
        self.assertTrue(self.om.consistent())

    def test_split_is_collateral_funded_and_no_new_capital(self):
        self.synced()
        self.assertTrue(self.om.split(frame()["market"], "5", "split"))
        state = self.om.ledger.state()
        self.assertEqual(state["cash"], "95.0")
        self.assertEqual(state["capital"], "100")
        self.assertEqual(balance(state, "a"), 5)
        self.assertTrue(self.om.consistent())
        self.assertFalse(self.om.split(frame()["market"], "5", "split"))

    def test_reduce_only_preserves_pairs_and_no_reverse_risk(self):
        f = self.synced()
        self.om.split(f["market"], "5", "split")
        reduce = self.session.observe(frame(self.om.now+1, period=4, remaining=120))
        decision = book_targets(reduce, self.om.account(), [], {}, self.policy)
        self.assertEqual(decision["targets"], [])  # paired shares cannot be dismantled
        account = copy.deepcopy(self.om.account())
        account["positions"]["a"] = "10"
        decision = book_targets(reduce, account, [], {}, self.policy)
        self.assertEqual({t["side"] for t in decision["targets"]}, {"SELL"})
        target = decision["targets"][0]
        target["size"] = "10"
        self.assertIn("exit_budget_slippage_or_portfolio_risk", submission_risk(target, reduce, account, [], self.policy, self.om.now))

    def test_clock_correction_and_delayed_status_stay_conservative(self):
        self.synced()
        corrected = self.session.observe(frame(self.om.now+1, remaining=610))
        self.assertEqual(corrected["phase"], "UNKNOWN")
        subsequent = self.session.observe(frame(self.om.now+1, remaining=609))
        self.assertEqual(subsequent["phase"], "UNKNOWN")

    def test_expected_unallocated_rebate_not_cash_and_missing_marks_unknown(self):
        self.synced()
        before = self.om.account()["cash"]
        self.om.income("estimate", "MAKER_REBATE", "EXPECTED", ".5", {"observed_ms": self.om.now, "provenance": "synthetic estimate"})
        self.assertEqual(self.om.account()["cash"], before)
        self.om.income("confirmation", "MAKER_REBATE", "CONFIRMED", ".5", {"observed_ms": self.om.now, "provenance": "fixture"})
        self.assertEqual(self.om.account()["cash"], before)
        from nba_recorder.fill_management.ledger import valuation
        self.om.split(frame()["market"], "5", "split")
        self.assertIsNone(valuation(self.om.ledger.state(), {})["net_pnl"])

    def test_predictive_fields_cannot_change_orders(self):
        f = self.synced()
        target = book_targets(f, self.om.account(), [], {}, self.policy)["targets"][0]
        for field in ("reversal", "final_result", "win_probability", "fair_probability"):
            invalid = {**f, field: "team a wins"}
            self.assertEqual(book_targets(invalid,self.om.account(),[],{},self.policy),book_targets(f,self.om.account(),[],{},self.policy))

    def test_single_writer_and_zero_network(self):
        with self.assertRaises(RuntimeError):
            SingleWriterStore(self.path)
        with patch("socket.socket", side_effect=AssertionError("no network")), patch("httpx.Client.request", side_effect=AssertionError("no HTTP")):
            self.opened()
            self.assertEqual(self.om.real_submission_calls, 0)

    def test_many_partial_same_side_fills_cannot_exceed_inventory_cap(self):
        self.synced()
        from nba_recorder.fair_value.maths import decimal
        for i in range(30):
            f = self.session.observe(frame(self.om.now+1, remaining=600-i))
            target = {"asset_id": "a", "market_id": "m", "event_id": "synthetic-event", "side": "BUY", "price": ".48", "size": "5", "expires_ms": self.om.now+1000, "maker_only": True, "purpose": "MAKER"}
            key = self.om.enqueue(f"partial-{i}", target, f)
            self.om.dispatch()
            self.om.advance(self.om.now+5)
            if self.om.order(key)["state"] == "OPEN":
                self.om.fill(key, "1", f"small-{i}")
                self.om.advance(self.om.now)
                self.om.cancel(key)
                self.om.advance(self.om.now+10)
            self.assertLessEqual(decimal(self.om.account()["positions"]["a"]), decimal(self.policy.max_asset_inventory))
            self.assertTrue(self.om.consistent())

    def test_models_fill_through_same_async_oms_with_different_quantities(self):
        outcomes = {}
        for model in ("L0", "L1", "L2"):
            other = SingleWriterStore(Path(self.tmp.name)/(model+".sqlite"))
            try:
                om = UnifiedPaperManager(other, replace(self.policy, enhanced=False), AT)
                session = BookSession(om, model)
                for offset in (0, 1):
                    session.on_event({"id": str(offset), "time_ms": AT+offset, "frames": [frame(AT+offset)], "trades": []})
                f = frame(AT+10)
                f["book"].update(bid=".45", ask=".47", bids=[{"price": ".45", "size": "100"}], asks=[{"price": ".47", "size": "4"}])
                session.on_event({"id": "touch-through", "time_ms": AT+10, "frames": [f], "trades": [{"event_id": "public", "asset_id": "a", "condition_id": "synthetic-condition", "side": "SELL", "price": ".46", "size": "103", "exchange_ms": AT+10}]})
                om.advance(AT+12)
                outcomes[model] = balance(om.ledger.state(), "a")
                self.assertTrue(om.consistent())
            finally:
                other.close()
        self.assertEqual(outcomes, {"L0": 4, "L1": 5, "L2": 3})

    def test_input_transaction_rolls_back_phase_orders_and_ledger(self):
        first = {"id": "one", "time_ms": AT, "frames": [frame(AT)], "trades": []}
        self.session.on_event(first)
        before = self.om.ledger.state()
        original_phase = self.om.phases.get("m")
        second = {"id": "two", "time_ms": AT+1, "frames": [frame(AT+1)], "trades": []}
        with patch.object(self.om, "mark_equity", side_effect=RuntimeError("crash point after dispatch")):
            with self.assertRaises(RuntimeError):
                self.session.on_event(second)
        self.assertEqual(self.om.orders(), [])
        self.assertEqual(self.om.phases.get("m"), original_phase)
        self.assertEqual(self.om.ledger.state(), before)
        self.assertFalse(self.store.db.execute("SELECT 1 FROM ip_inputs WHERE id='two'").fetchone())
        self.session.on_event(second)
        self.assertEqual(len(self.om.orders()), 1)

    def test_os_exit_unreceived_fill_reconciles_into_same_ledger(self):
        import subprocess
        import sys
        path = str(Path(self.tmp.name)/"killed.sqlite")
        code = """
import os, sys
from nba_recorder.inplay.store import SingleWriterStore
from nba_recorder.inplay.manager import UnifiedPaperManager
from nba_recorder.inplay.session import BookSession
from nba_recorder.inplay.demo import AT, frame
store=SingleWriterStore(sys.argv[1])
om=UnifiedPaperManager(store,start_ms=AT)
s=BookSession(om)
s.observe(frame(AT))
f=s.observe(frame(AT+1))
s.quote(f,'q')
om.advance(AT+6)
order=om.orders()[0]
om.fill(order['id'],'2','unreceived',transport_ms=30)
os._exit(23)
"""
        child = subprocess.run([sys.executable, "-c", code, path], capture_output=True, timeout=15)
        self.assertEqual(child.returncode, 23, child.stderr.decode())
        other = SingleWriterStore(path)
        try:
            om = UnifiedPaperManager(other)
            self.assertTrue(om.halted)
            om.advance(om.now+40)
            self.assertTrue(om.reconcile())
            self.assertEqual(balance(om.ledger.state(), "a"), 2)
            self.assertTrue(om.consistent())
            self.assertTrue(om.halted)
        finally:
            other.close()

    def test_collection_scope_is_not_quote_permission(self):
        from test_core import fixture as market_fixture
        from nba_recorder.api import select_market
        event, market, chosen = market_fixture()
        event.update(live=True, ended=False)
        self.assertIsNone(select_market(event, market, "10345", chosen["game_start_ms"])[0])
        live, _ = select_market(event, market, "10345", chosen["game_start_ms"], scope="full-game")
        self.assertIsNotNone(live)
        self.assertNotIn("phase", live)
        event.update(ended=True)
        market["closed"] = True
        self.assertIsNotNone(select_market(event, market, "10345", chosen["game_start_ms"], scope="full-game")[0])

    def test_raw_sports_without_semantic_evidence_cannot_authorize(self):
        from nba_recorder.inplay.sports import normalize
        raw = {"gameId": 7, "leagueAbbreviation": "NBA", "homeTeam": "Detroit Pistons", "awayTeam": "Boston Celtics", "status": "InProgress", "live": True, "ended": False, "period": "4Q", "elapsed": "02:30"}
        contract = {"game_id": 7, "event_id": "synthetic-event", "home_team": "det", "away_team": "bos", "identity_evidence": "fixture"}
        normalized = normalize(raw, AT, contract)
        self.assertEqual(normalized["period"], 4)
        self.assertEqual(normalized["clock_semantics"], "UNVERIFIED")
        self.assertIsNone(normalized["paused"])
        self.assertIsNone(normalized["observed_ms"])
        f = frame()
        f["sports"] = normalized
        self.assertEqual(self.session.observe(f)["phase"], "UNKNOWN")

    def test_no_future_frame_and_no_fill_model_changes(self):
        with self.assertRaises(ValueError):
            BookSession(self.om, "L2")
        future = frame(AT+1)
        with self.assertRaises(ValueError):
            self.session.on_event({"id": "future", "time_ms": AT, "frames": [future], "trades": []})

    def test_income_cash_flow_identity_and_costs_reconcile(self):
        self.synced()
        evidence = {"observed_ms": self.om.now, "provenance": "explicit synthetic transfer", "cash_flow_id": "one"}
        self.om.income("receipt", "MAKER_REBATE", "RECEIVED", "1", evidence)
        from nba_recorder.fill_management.ledger import valuation
        self.assertEqual(self.om.account()["cash"], "101")
        with self.assertRaises(sqlite3.IntegrityError):
            self.om.income("duplicate", "MAKER_REBATE", "RECEIVED", "1", evidence)
        self.om.income("cost", "SIMULATED_COST", "RECEIVED", ".2", {**evidence, "cash_flow_id": "two"})
        self.assertEqual(valuation(self.om.ledger.state(), {})["net_pnl"], "0.8")
        self.assertTrue(self.om.consistent())

    def test_book_freeze_respects_processing_and_future_metadata(self):
        from test_core import fixture as market_fixture, full
        from nba_recorder.storage import Store
        from nba_recorder.book import Book
        from nba_recorder.quote_engine.storage import PaperStore
        from nba_recorder.inplay.tape import freeze
        source_path = Path(self.tmp.name)/"source.sqlite"
        source = Store(source_path)
        tape = PaperStore(Path(self.tmp.name)/"tape.sqlite")
        try:
            _, _, market = market_fixture()
            source.markets([market], AT)
            source.health(AT, "connected")
            raw = full(str(AT))
            book = Book(market, "a")
            book.apply(raw, AT)
            with patch("nba_recorder.storage.time.time_ns", return_value=(AT+100)*1000000):
                source.commit(raw, "ws", AT, [book.snapshot(AT, 30)])
            identity = freeze(tape, source_path, AT, AT+200, step_ms=50)
            rows = [json.loads(r[0]) for r in tape.db.execute("SELECT payload FROM qe_input WHERE tape_id=? ORDER BY seq", (identity,))]
            self.assertIsNone(rows[0]["frames"][0]["book"])
            hundred = next(e for e in rows if e["time_ms"] == AT+100)
            self.assertIsNotNone(hundred["frames"][0]["book"])
            self.assertEqual(hundred["frames"][0]["processed_ms"], AT+100)
            source.markets([{**market, "question": "future metadata"}], AT+201)
            self.assertEqual(freeze(tape, source_path, AT, AT+200, step_ms=50), identity)
        finally:
            source.close()
            tape.close()

    def test_book_realized_decomposition_is_not_predictive_edge(self):
        from nba_recorder.fill_management.ledger import Ledger, valuation
        ledger = Ledger(self.store.db, "book-attribution-test", "test")
        ledger.add("capital", AT, "CAPITAL", {"amount": "100"})
        meta = {"asset_id": "z", "market_id": "test", "role": "YES", "label": "Team", "complement": "other"}
        ledger.add("mapping", AT, "ASSET", meta)
        ledger.add("buy", AT, "FILL", {**meta, "side": "BUY", "price": ".48", "quantity": "2", "context": {"strategy_mode": "BOOK_DRIVEN", "reference_mid": ".5"}})
        ledger.add("sell", AT+1, "FILL", {**meta, "side": "SELL", "price": ".53", "quantity": "1", "context": {"strategy_mode": "BOOK_DRIVEN", "reference_mid": ".52"}})
        attribution = ledger.state()["closures"][0]["attribution"]
        self.assertTrue(attribution["book_additive_reconciliation"])
        self.assertIsNone(attribution["entry_edge"])
        self.assertEqual(attribution["book_decomposition"]["entry_quote_premium"], "0.02")
        self.assertEqual(valuation(ledger.state(), {"z": ".5"})["net_pnl"], "0.07")

    def test_missing_book_with_resting_orders_halts_without_losing_risk_event(self):
        self.opened()
        f = frame(self.om.now+1)
        f["book"] = None
        self.session.on_event({"id": "missing-book", "time_ms": f["time_ms"], "frames": [f], "trades": []})
        self.assertEqual(self.om.risk_state, "HALTED")
        self.assertTrue(self.store.db.execute("SELECT 1 FROM pe_risks").fetchone())
        # The touch path must also tolerate missing books after a cancel request.
        self.session.model = "L0"
        self.session.executions([f], [], "missing-touch")
        self.assertEqual(balance(self.om.ledger.state(), "a"), 0)
