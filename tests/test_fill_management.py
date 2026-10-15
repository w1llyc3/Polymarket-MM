import copy
import json
import tempfile
import unittest
from decimal import Decimal, getcontext
from pathlib import Path
from unittest.mock import patch

from test_quote_engine import AT, frame, event, trade
from nba_recorder.fill_management.engine import ResearchExecutor
from nba_recorder.fill_management.exits import evaluate_lot
from nba_recorder.fill_management.ledger import Ledger, balance, valuation
from nba_recorder.fill_management.models import ModelConfig
from nba_recorder.fill_management.report import daily_report
from nba_recorder.fill_management.storage import initialize
from nba_recorder.fill_management.paper_import import import_paper
from nba_recorder.paper_execution.store import ExecutionStore
from nba_recorder.paper_execution.manager import OrderManager
from nba_recorder.paper_execution.risk import ExecutionPolicy
from nba_recorder.quote_engine.model import Policy
from nba_recorder.quote_engine.storage import PaperStore


def research_frame(at=AT, bid=".49", ask=".51", depth="4"):
    f = frame(at, bid, ask)
    f["market"]["outcomes"] = [{"asset_id": "a", "label": "YES"}, {"asset_id": "b", "label": "NO"}]
    f["book"]["bids"][0]["size"] = depth
    f["book"]["asks"][0]["size"] = depth
    return f


class AccountingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/"ledger.sqlite"
        self.store = PaperStore(self.path)
        initialize(self.store.db)
        self.ledger = Ledger(self.store.db, "account", "L2")
        self.ledger.add("capital", AT, "CAPITAL", {"amount": "100"})
        for asset, other in (("a", "b"), ("b", "a")):
            self.ledger.add("seed-"+asset, AT, "SEED", {"asset_id": asset, "market_id": "m", "role": "YES" if asset == "a" else "NO", "complement": other, "quantity": "0", "price": "0"})

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def fill(self, eid, asset="a", qty="5", price=".4", side="BUY", fee="0", at=None, **extra):
        payload = {"asset_id": asset, "market_id": "m", "role": "YES" if asset == "a" else "NO", "complement": "b" if asset == "a" else "a", "side": side,
                   "quantity": qty, "price": price, "fee": fee, "order_id": "order-"+eid, "context": {"fair_probability": ".5"}, **extra}
        self.ledger.add(eid, AT+1 if at is None else at, "FILL", payload)
        return payload

    def test_partial_fill_duplicate_and_cash_reservation(self):
        p = self.fill("partial", qty="2", fee=".01")
        self.assertFalse(self.ledger.add("partial", AT+1, "FILL", p))
        with self.assertRaises(ValueError):
            self.ledger.add("partial", AT+1, "FILL", {**p, "quantity": "3"})
        state = self.ledger.state()
        active = [{"state": "OPEN", "side": "BUY", "price": ".4", "size": "5", "remaining": "3"}]
        marked = valuation(state, {"a": ".5"}, active)
        self.assertEqual(Decimal(marked["reserved_cash"]), Decimal("1.2"))
        self.assertEqual(Decimal(marked["net_pnl"]), Decimal(".19"))
        self.assertEqual(balance(state, "a"), 2)

    def test_multiple_batches_fifo_partial_exit(self):
        self.fill("batch1", qty="3", price=".4")
        self.fill("batch2", qty="4", price=".5", at=AT+2)
        self.fill("exit", qty="5", price=".6", side="SELL", at=AT+3)
        state = self.ledger.state()
        self.assertEqual(balance(state, "a"), 2)
        self.assertEqual(Decimal(state["realized_trading_pnl"]), Decimal(".8"))
        marked = valuation(state, {"a": ".55"})
        self.assertEqual(Decimal(marked["unrealized_trading_pnl"]), Decimal(".1"))
        self.assertEqual(Decimal(marked["net_pnl"]), Decimal(".9"))
        self.assertEqual(len(state["closures"]), 2)
        for c in state["closures"]:
            self.assertEqual(sum(Decimal(c["attribution"][k]) for k in ("entry_edge", "spread_capture", "directional_residual")), Decimal(c["gross_pnl"]))

    def test_complete_share_cost_below_equal_above_one(self):
        for no_price, expected in ((".4", ".5"), (".5", "0"), (".6", "-.5")):
            lane = "cost-"+no_price
            ledger = Ledger(self.store.db, "account", lane)
            ledger.add(lane+"capital", AT, "CAPITAL", {"amount": "100"})
            for asset, price in (("a", ".5"), ("b", no_price)):
                ledger.add(lane+asset, AT+1, "FILL", {"asset_id": asset, "market_id": "m", "complement": "b" if asset == "a" else "a", "role": "YES" if asset == "a" else "NO", "side": "BUY", "quantity": "5", "price": price, "context": {"fair_probability": ".5"}})
            before = valuation(ledger.state(), {"a": ".5", "b": ".5"})
            ledger.add(lane+"pair", AT+2, "PAIR", {"yes_asset": "a", "no_asset": "b", "quantity": "5", "evidence": {"observed_ms": AT, "data_kind": "synthetic"}})
            after = valuation(ledger.state(), {})
            self.assertEqual(Decimal(after["net_pnl"]), Decimal(expected))
            self.assertEqual(after["equity"], before["equity"])
            self.assertEqual(Decimal(after["pair_cash_flow"]), 5)

    def test_partial_hedge_remains_directional(self):
        self.fill("entry", qty="5")
        self.ledger.add("hedge-intent", AT+2, "HEDGE_INTENT", {"lot_id": "entry", "asset_id": "b", "quantity": "5"})
        self.fill("hedge", asset="b", qty="2", price=".5", at=AT+3, purpose="HEDGE", hedge_intent_id="hedge-intent")
        state = self.ledger.state()
        self.assertEqual(balance(state, "a")-balance(state, "b"), 3)
        self.assertEqual(Decimal(state["hedge_intents"]["hedge-intent"]["filled"]), 2)
        with self.assertRaises(ValueError):
            self.fill("over-hedge", asset="b", qty="4", at=AT+4, purpose="HEDGE", hedge_intent_id="hedge-intent")

    def test_settlement_equity_and_rewards_and_fees(self):
        self.fill("yes", qty="5", price=".4", fee=".01")
        self.fill("no", asset="b", qty="2", price=".5", fee=".02", at=AT+2)
        self.ledger.add("reward", AT+3, "REWARD", {"amount": ".03", "evidence": {"data_kind": "synthetic", "observed_ms": AT+3}})
        before = valuation(self.ledger.state(), {"a": "1", "b": "0"})
        self.ledger.add("settlement", AT+4, "SETTLEMENT", {"market_id": "m", "payouts": {"a": "1", "b": "0"}, "evidence": {"observed_ms": AT+4, "data_kind": "synthetic"}})
        after = valuation(self.ledger.state(), {})
        self.assertEqual(Decimal(after["net_pnl"]), 2)
        self.assertEqual(after["equity"], before["equity"])
        self.assertEqual(Decimal(after["settlement_cash_flow"]), 5)
        self.assertTrue(after["reconciled"])

    def test_restart_and_unknown_valuation_and_future_events(self):
        self.fill("entry")
        before = valuation(self.ledger.state(), {"a": ".5"})
        self.store.close()
        self.store = PaperStore(self.path)
        self.ledger = Ledger(self.store.db, "account", "L2")
        self.assertTrue(self.ledger.audit())
        self.assertEqual(before, valuation(self.ledger.state(), {"a": ".5"}))
        self.assertIsNone(valuation(self.ledger.state(), {})["net_pnl"])
        self.assertEqual(balance(self.ledger.rebuild(AT), "a"), 0)
        with self.assertRaises(ValueError):
            self.fill("past", at=AT-1)
        with self.assertRaises(ValueError):
            self.ledger.add("future-settle", AT+2, "SETTLEMENT", {"market_id": "m", "payouts": {"a": "1", "b": "0"}, "evidence": {"observed_ms": AT+3}})

    def test_unknown_seed_basis_cannot_claim_pnl(self):
        self.ledger.add("unpriced-seed", AT+1, "SEED", {"asset_id": "a", "market_id": "m", "complement": "b", "quantity": "5", "price": None})
        result = valuation(self.ledger.state(), {"a": ".5"})
        self.assertEqual(Decimal(result["equity"]), Decimal("102.5"))
        self.assertIsNone(result["net_pnl"])
        self.assertFalse(result["cost_basis_complete"])

    def test_decimal_context_and_insufficient_cash(self):
        self.fill("precise", price=".4321", qty="9.99", fee=".00001")
        a = valuation(self.ledger.state(), {"a": ".5432"})
        old = getcontext().prec
        try:
            getcontext().prec = 6
            self.assertEqual(a, valuation(self.ledger.state(), {"a": ".5432"}))
        finally:
            getcontext().prec = old
        with self.assertRaises(ValueError):
            self.fill("too-large", qty="1000")

    def test_asymmetric_fair_value_attribution(self):
        self.fill("asym", qty="9.99", context={"fair_probability": ".432123412341234123412341234123412341234123"})
        self.fill("sell-asym", qty="9.99", price=".6", side="SELL", at=AT+2, context={"fair_probability": ".563563563563563563563563563563563563563563"})
        self.assertTrue(self.ledger.audit())
        self.assertEqual(Decimal(self.ledger.state()["realized_trading_pnl"]), Decimal("1.998"))

    def test_partial_pair_then_settlement_is_not_double_payout(self):
        self.fill("a", qty="5", price=".4")
        self.fill("b", asset="b", qty="3", price=".5", at=AT+2)
        self.ledger.add("pair", AT+3, "PAIR", {"yes_asset": "a", "no_asset": "b", "quantity": "2", "evidence": {"observed_ms": AT+3}})
        self.ledger.add("resolve", AT+4, "SETTLEMENT", {"market_id": "m", "payouts": {"a": "1", "b": "0"}, "evidence": {"observed_ms": AT+4}})
        result = valuation(self.ledger.state(), {})
        self.assertEqual(Decimal(result["pair_cash_flow"]), 2)
        self.assertEqual(Decimal(result["settlement_cash_flow"]), 3)
        self.assertEqual(Decimal(result["net_pnl"]), Decimal("1.5"))


class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/"research.sqlite"
        self.store = PaperStore(self.path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def engine(self, events, **config):
        tape = self.store.seal(events, {"start_ms": events[0]["time_ms"], "end_ms": events[-1]["time_ms"]})
        return ResearchExecutor(self.store, tape, Policy(), ModelConfig(**config))

    def test_l2_partial_fill_then_cancel_and_three_models_differ(self):
        events = [event(research_frame()), event(research_frame(AT+1000), [trade(AT+1000, price=".49", size="6")])]
        engine = self.engine(events)
        engine.run(close=True)
        self.assertEqual(balance(engine.ledger("1c-L2").state(), "a"), 2)
        self.assertEqual(balance(engine.ledger("1c-L1").state(), "a"), 0)
        self.assertEqual(balance(engine.ledger("1c-L0").state(), "a"), 0)
        self.assertTrue(self.store.db.execute("SELECT 1 FROM qe_journal WHERE lane='1c-L2' AND kind='PARTIALLY_FILLED'").fetchone())
        self.assertTrue(self.store.db.execute("SELECT 1 FROM qe_orders WHERE lane='1c-L2' AND state='CANCELLED'").fetchone())
        engine.audit()

    def test_all_three_models_have_distinct_fill_quantities(self):
        engine = self.engine([event(research_frame()), event(research_frame(AT+1000, ".48", ".49")), event(research_frame(AT+1500), [trade(AT+1500, price=".47", size="6")])])
        engine.run(close=True)
        totals = [balance(engine.ledger("1c-"+model).state(), "a") for model in ("L0", "L1", "L2")]
        self.assertEqual(totals, [Decimal(4), Decimal(5), Decimal(2)])

    def test_future_depth_never_changes_initial_queue_and_duplicates(self):
        f = research_frame(AT+1000, depth="0")
        t = trade(AT+1000, price=".49", size="2")
        engine = self.engine([event(research_frame()), event(f, [t]), event(research_frame(AT+2000, depth="0"), [t])])
        engine.run()
        self.assertEqual(balance(engine.ledger("1c-L2").state(), "a"), 0)
        order = next(o for o in engine.orders("1c-L2") if o["state"] in {"OPEN", "PARTIALLY_FILLED"})
        self.assertEqual(Decimal(order["queue_ahead"]), 2)

    def test_model_configuration_and_fees_are_traceable(self):
        engine = self.engine([event(research_frame()), event(research_frame(AT+1000), [trade(AT+1000, size="6")])], queue_multiplier="2", hidden_ahead="1", maker_fee_rate=".01")
        engine.run(close=True)
        self.assertEqual(balance(engine.ledger("1c-L2").state(), "a"), 0)
        state = engine.ledger("1c-L1").state()
        self.assertGreater(Decimal(state["fees"]), 0)
        self.assertEqual(Decimal(state["cash"]), Decimal(engine.account("1c-L1")["cash"]))
        fill = json.loads(self.store.db.execute("SELECT payload FROM l6_events WHERE lane='1c-L1' AND kind='FILL'").fetchone()[0])
        self.assertEqual(fill["evidence"]["assumptions"]["confidence"], "low_uncalibrated")

    def test_report_net_pnl_matches_cash_positions_and_daily_rates(self):
        engine = self.engine([event(research_frame()), event(research_frame(AT+1000), [trade(AT+1000, size="6")]), event(research_frame(AT+11000))])
        engine.run(close=True)
        report = daily_report(self.store.db, engine.run_id, "2026-10-14")
        self.assertEqual(len(report["lanes"]), 9)
        for lane in report["lanes"]:
            account = lane["closing_account"]
            self.assertEqual(Decimal(lane["daily_net_pnl"]), Decimal(account["cash"])+Decimal(account["market_value"])-Decimal(account["capital_contributions"]))
            self.assertEqual(Decimal(lane["submitted_quantity"]), Decimal(lane["filled_quantity"])+Decimal(lane["pending_quantity"])+Decimal(lane["cancelled_or_expired_quantity"]))
            self.assertIsNone(lane["hedge_success_rate"])
            self.assertTrue(lane["attribution"]["overlapping_metrics_never_summed"])
        self.assertEqual(report["real_submission_calls"], 0)

    def test_restart_preserves_equity_and_queue(self):
        engine = self.engine([event(research_frame()), event(research_frame(AT+1000), [trade(AT+1000, size="6")]), event(research_frame(AT+2000))])
        engine.run(max_frames=2)
        states = {lane: engine.ledger(lane).state() for lane, _, _ in engine.lanes}
        self.store.close()
        self.store = PaperStore(self.path)
        resumed = ResearchExecutor(self.store, engine.tape_id)
        for lane, _, _ in resumed.lanes:
            self.assertEqual(states[lane], resumed.ledger(lane).state())
        resumed.run(close=True)
        self.assertTrue(resumed.audit()["fifo_reconciled"])

    def test_zero_live_calls_and_unavailable_market_is_no_quote(self):
        bad = research_frame()
        bad["connected"] = False
        engine = self.engine([event(bad)])
        with patch("socket.socket", side_effect=AssertionError("network forbidden")), patch("httpx.Client", side_effect=AssertionError("network forbidden")):
            engine.run(close=True)
        self.assertEqual(engine.audit()["orders"], 0)
        self.assertEqual(engine.audit()["real_submission_calls"], 0)

    def test_current_markout_cannot_see_future_input(self):
        engine = self.engine([event(research_frame()), event(research_frame(AT+1000), [trade(AT+1000, size="6")]), event(research_frame(AT+11000))])
        engine.run(max_frames=2)
        report = daily_report(self.store.db, engine.run_id, "2026-10-14")
        lane = next(l for l in report["lanes"] if l["lane"] == "1c-L1")
        self.assertIsNone(lane["markouts"]["10"]["quantity_weighted_per_share"])
        engine.run()
        report = daily_report(self.store.db, engine.run_id, "2026-10-14")
        lane = next(l for l in report["lanes"] if l["lane"] == "1c-L1")
        self.assertIsNotNone(lane["markouts"]["10"]["quantity_weighted_per_share"])

    def test_equivalent_decimal_grid_not_cancelled_and_close_keeps_peak(self):
        f1, f2 = research_frame(), research_frame(AT+1000)
        f2["book"]["tick_size"] = ".010"
        f2["book"]["minimum_order_size"] = "5.0"
        engine = self.engine([event(f1), event(f2)])
        engine.run(close=True)
        orders = engine.orders("1c-L1")
        self.assertEqual(len(orders), 1)
        report = daily_report(self.store.db, engine.run_id, "2026-10-14")
        lane = next(l for l in report["lanes"] if l["lane"] == "1c-L1")
        self.assertEqual(Decimal(lane["closing_account"]["reserved_cash"]), 0)
        self.assertEqual(Decimal(lane["max_conservative_committed_exposure"]), 5)

    def test_exit_candidates_consider_depth_fee_and_cash(self):
        engine = self.engine([event(research_frame()), event(research_frame(AT+1000), [trade(AT+1000, size="6")])], taker_fee_rate=".05")
        engine.run()
        state = engine.ledger("1c-L1").state()
        lot = next(l for l in state["lots"] if l.get("remaining") == "5")
        a = research_frame(AT+1000, depth="100")
        b = copy.deepcopy(a)
        b["asset_id"] = b["book"]["asset_id"] = b["signal"]["asset_id"] = "b"
        plans = evaluate_lot(lot, state, {"a": a, "b": b}, AT+1000, ModelConfig(taker_fee_rate=".05"), available_cash="100")
        self.assertEqual({p["status"] for p in plans}, {"EXIT_CANDIDATE", "HEDGE_CANDIDATE", "HOLD_CANDIDATE"})
        unknown = evaluate_lot(lot, state, {"a": a, "b": b}, AT+1000, ModelConfig(), available_cash="100")
        self.assertTrue(any("unknown_taker_fee" in p["reasons"] for p in unknown))
        poor = evaluate_lot(lot, state, {"a": a, "b": b}, AT+1000, ModelConfig(taker_fee_rate=".05"), available_cash="0")
        self.assertIn("cash_unavailable_or_insufficient", next(p for p in poor if p["plan"] == "HEDGE")["reasons"])
        b["book"]["asks"][0]["size"] = "2"
        partial = evaluate_lot(lot, state, {"a": a, "b": b}, AT+1000, ModelConfig(taker_fee_rate="0"), available_cash="100")
        hedge = next(p for p in partial if p["plan"] == "HEDGE")
        self.assertEqual(hedge["status"], "MANUAL_REVIEW")
        self.assertEqual(Decimal(hedge["paired_quantity"]), 2)
        self.assertEqual(Decimal(hedge["unhedged_residual_inventory"]), 3)
        self.assertEqual(Decimal(hedge["conditional_complete_pair_value"]), Decimal(".98"))

    def test_existing_complementary_inventory_not_bought_twice(self):
        engine = self.engine([event(research_frame()), event(research_frame(AT+1000), [trade(AT+1000, size="6")])])
        engine.run(close=True)
        state = engine.ledger("1c-L1").state()
        lot = next(l for l in state["lots"] if not l.get("seed"))
        state["lots"].append({"id": "already-owned-no", "asset_id": "b", "remaining": "5", "price": ".5", "market_id": "m", "context": {}})
        a = research_frame(AT+1000, depth="100")
        b = copy.deepcopy(a)
        b["asset_id"] = b["book"]["asset_id"] = b["signal"]["asset_id"] = "b"
        plans = evaluate_lot(lot, state, {"a": a, "b": b}, AT+1000, ModelConfig(taker_fee_rate=".05"), available_cash="100")
        hedge = next(p for p in plans if p["plan"] == "HEDGE")
        self.assertEqual(hedge["status"], "HOLD_CANDIDATE")
        self.assertEqual(Decimal(hedge["additional_complement_required"]), 0)
        self.assertEqual(Decimal(hedge["cash_required"]), 0)

    def test_native_yes_no_order_and_unseen_complement_zero_position(self):
        f = research_frame()
        f["market"]["outcomes"] = [{"asset_id": "b", "label": "No"}, {"asset_id": "a", "label": "Yes"}]
        engine = self.engine([event(f)])
        engine.run(close=True)
        state = engine.ledger("1c-L2").state()
        self.assertEqual(state["assets"]["a"]["role"], "YES")
        self.assertEqual(state["assets"]["b"]["role"], "NO")
        self.assertEqual(balance(state, "b"), 0)

    def test_paper_import_idempotent_and_read_only_source(self):
        path = Path(self.tmp.name)/"paper-source.sqlite"
        source = ExecutionStore(path)
        try:
            om = OrderManager(source, ExecutionPolicy(), AT)
            f = research_frame()
            order = om.enqueue("paper-order", {"price": ".49", "size": "5", "side": "BUY", "expires_ms": AT+30000}, f)
            om.dispatch()
            om.advance(AT+5)
            om.fill(order, "2", "paper-fill", transport_ms=3)
            om.advance(AT+8)
            om.cancel(order)
            om.advance(AT+18)
            om.kill("end")
            count = source.db.execute("SELECT count(*) FROM pe_journal").fetchone()[0]
            first = import_paper(self.store, path)
            events = self.store.db.execute("SELECT count(*) FROM l6_events").fetchone()[0]
            second = import_paper(self.store, path)
            self.assertEqual(first, second)
            self.assertEqual(events, self.store.db.execute("SELECT count(*) FROM l6_events").fetchone()[0])
            self.assertEqual(count, source.db.execute("SELECT count(*) FROM pe_journal").fetchone()[0])
            self.assertTrue(first["account"]["reconciled"])
            self.assertEqual(Decimal(first["account"]["positions"]["a"]), 2)
        finally:
            source.close()

    def test_crash_rollback_does_not_leave_fill_in_fifo_ledger(self):
        engine = self.engine([event(research_frame()), event(research_frame(AT+1000), [trade(AT+1000, size="6")])])
        engine.run(max_frames=1)
        original = engine.step
        def failed(row):
            original(row)
            raise RuntimeError("crash before cursor commit")
        with patch.object(engine, "step", side_effect=failed), self.assertRaises(RuntimeError):
            engine.run()
        self.assertEqual(balance(engine.ledger("1c-L1").state(), "a"), 0)
        self.assertEqual(self.store.db.execute("SELECT cursor FROM qe_runs").fetchone()[0], 1)
        engine.run(close=True)
        self.assertTrue(engine.audit()["fifo_reconciled"])

    def test_cross_day_equity_and_retrospective_markouts(self):
        boundary = (AT//86400000+1)*86400000
        times = [boundary-3000, boundary-2000, boundary-1000, boundary+1000, boundary+8000]
        events = []
        for at in times:
            f = research_frame(at, ".59" if at > boundary else ".49", ".61" if at > boundary else ".51")
            f["market"]["game_start_ms"] = boundary+5*86400000
            events.append(event(f, [trade(at, size="6")] if at == times[1] else []))
        engine = self.engine(events)
        engine.run(close=True)
        first = daily_report(self.store.db, engine.run_id, "2026-10-14")
        second = daily_report(self.store.db, engine.run_id, "2026-10-15")
        a = next(l for l in first["lanes"] if l["lane"] == "1c-L1")
        b = next(l for l in second["lanes"] if l["lane"] == "1c-L1")
        self.assertEqual(Decimal(a["daily_net_pnl"]), Decimal(".05"))
        self.assertEqual(Decimal(b["daily_net_pnl"]), Decimal(".5"))
        self.assertEqual(b["opening_equity"], a["closing_account"]["equity"])
        self.assertIsNotNone(a["markouts"]["10"]["quantity_weighted_per_share"])

    def test_closed_run_partial_hedge_and_settlement_reports_reconcile(self):
        last = event(research_frame(AT+1000), [trade(AT+1000, size="6")])
        complement = research_frame(AT+1000)
        complement["asset_id"] = complement["book"]["asset_id"] = complement["signal"]["asset_id"] = "b"
        last["frames"].append(complement)
        engine = self.engine([event(research_frame()), last])
        engine.run(close=True)
        state = engine.ledger("1c-L1").state()
        lot = next(l for l in state["lots"] if not l.get("seed"))
        intent = {"id": "manual-intent", "time_ms": AT+1100, "kind": "HEDGE_INTENT", "data_kind": "synthetic", "payload": {"lot_id": lot["id"], "asset_id": "b", "quantity": "5"}}
        self.assertTrue(engine.record_paper_event("1c-L1", intent))
        fill = {"id": "manual-hedge", "time_ms": AT+1200, "kind": "FILL", "data_kind": "synthetic", "payload": {"asset_id": "b", "market_id": "m", "side": "BUY", "quantity": "2", "price": ".5", "fee": ".02", "purpose": "HEDGE", "hedge_intent_id": "manual-intent", "context": {"fair_probability": ".5", "expected_hedge_price": ".49"}}}
        self.assertTrue(engine.record_paper_event("1c-L1", fill))
        self.assertFalse(engine.record_paper_event("1c-L1", fill))
        report = daily_report(self.store.db, engine.run_id, "2026-10-14")
        lane = next(l for l in report["lanes"] if l["lane"] == "1c-L1")
        self.assertEqual(Decimal(lane["hedge_success_rate"]), Decimal("0"))
        self.assertEqual(Decimal(lane["hedge_quantity_completion_rate"]), Decimal(".4"))
        self.assertEqual(Decimal(lane["inventory"]["m"]["directional_quantity"]), 3)
        self.assertEqual(Decimal(lane["attribution"]["hedge_slippage"]), Decimal(".02"))
        self.assertTrue(lane["net_pnl_reconciled"])
        settle = {"id": "resolve", "time_ms": AT+1300, "kind": "SETTLEMENT", "data_kind": "synthetic", "payload": {"market_id": "m", "payouts": {"a": "1", "b": "0"}, "evidence": {"observed_ms": AT+1300, "data_kind": "synthetic"}}}
        engine.record_paper_event("1c-L1", settle)
        report = daily_report(self.store.db, engine.run_id, "2026-10-14")
        lane = next(l for l in report["lanes"] if l["lane"] == "1c-L1")
        self.assertEqual(Decimal(lane["closing_account"]["market_value"]), 0)
        self.assertEqual(Decimal(lane["daily_net_pnl"]), Decimal("1.53"))
        engine.audit()
