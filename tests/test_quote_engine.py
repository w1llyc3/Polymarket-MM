import copy
import json
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from nba_recorder.fair_value.demo import AT, market, quote, observation
from nba_recorder.fair_value.engine import evaluate
from nba_recorder.fair_value.providers import OddsBatch
from nba_recorder.fair_value.store import FairStore
from nba_recorder.quote_engine.executor import Executor, LANES
from nba_recorder.quote_engine.model import Policy, targets
from nba_recorder.quote_engine.storage import PaperStore
from nba_recorder.quote_engine.tape import freeze


def frame(at=AT, bid="0.49", ask="0.51", ref=None):
    return {"time_ms": at, "market_id": "m", "asset_id": "a", "market": {**market(), "market_id": "m"}, "connected": True,
            "book_received_ms": at, "book_ref": ref or str(at),
            "book": {"market_id": "m", "asset_id": "a", "bid": bid, "ask": ask, "tick_size": "0.01", "minimum_order_size": "5", "flags": [], "exchange_timestamp": at,
                     "bids": [{"price": bid, "size": "100"}], "asks": [{"price": ask, "size": "100"}]},
            "signal": {"id": str(at), "decision_ms": at, "market_id": "m", "asset_id": "a", "status": "RESEARCH_SIGNAL", "reference_eligible": True, "fair_probability": "0.5", "market_observed_ms": at,
                       "config": {"metadata_max_age_ms": 360000, "book_max_age_ms": 30000, "odds_max_age_ms": 120000},
                       "sources": [{"source_id": x, "included": True, "updated_ms": at, "received_ms": at} for x in ("s1", "s2")]}}


def event(f, trades=None):
    return {"time_ms": f["time_ms"], "frames": [f], "trades": trades or []}


def trade(at, price="0.47", side="SELL", size="2", key="unique"):
    return {"event_key": key, "index": 0, "receive_ms": at, "payload": {"asset_id": "a", "market": "synthetic-condition", "price": price, "side": side, "size": size, "timestamp": str(at)}}


class QuoteTests(unittest.TestCase):
    def target(self, f=None, cash="1000", inventory="10", **policy):
        return targets(f or frame(), {"cash": cash, "positions": {"a": inventory}}, [], {"a": {"event_id": "synthetic-event"}}, "0.01", Policy(target_inventory="10", **policy))

    def test_four_states(self):
        self.assertEqual(self.target()["state"], "TWO_SIDED")
        self.assertEqual(self.target(inventory="0")["state"], "BUY_ONLY")
        self.assertEqual(self.target(cash="0")["state"], "SELL_ONLY")
        self.assertEqual(self.target(cash="0", inventory="0")["state"], "NO_QUOTE")

    def test_supported_ticks_and_maker_constraints(self):
        for tick in ("0.1", "0.01", "0.005", "0.0025", "0.001", "0.0001"):
            f = frame()
            f["book"]["tick_size"] = tick
            for t in self.target(f)["targets"]:
                self.assertEqual(Decimal(t["price"]) % Decimal(tick), 0)
                self.assertGreaterEqual(Decimal(t["price"]), Decimal(tick))
                self.assertLessEqual(Decimal(t["price"]), 1-Decimal(tick))
                self.assertTrue(Decimal(t["price"]) < Decimal(".51") if t["side"] == "BUY" else Decimal(t["price"]) > Decimal(".49"))
                self.assertGreaterEqual(Decimal(t["gross_edge"]), Decimal(".01"))

    def test_stale_future_and_missing_sources(self):
        for mutation in (lambda f: f.update(connected=False), lambda f: f["signal"].update(market_observed_ms=AT-400000), lambda f: f["signal"]["sources"][0].update(updated_ms=AT-200000), lambda f: f["book"].update(exchange_timestamp=AT+1), lambda f: f["signal"].update(sources=[]), lambda f: f["signal"].update(reference_eligible=False)):
            f = frame()
            mutation(f)
            self.assertEqual(self.target(f)["state"], "NO_QUOTE")

    def test_bad_market_and_start(self):
        for mutate in (lambda f: f["market"].update(event_live=True), lambda f: f["market"]["status"].update(closed=True), lambda f: f["market"].update(game_start_ms=AT+300000), lambda f: f["book"].update(bid=".52"), lambda f: f["book"].update(ask=".49"), lambda f: f["book"].update(bid=None), lambda f: f["book"].update(tick_size=".03"), lambda f: f["book"].update(flags=["unverified_continuity"])):
            f = frame()
            mutate(f)
            self.assertEqual(self.target(f)["state"], "NO_QUOTE")

    def test_risk_limits_and_minimum_size(self):
        self.assertEqual(self.target(max_order_notional="1")["state"], "NO_QUOTE")
        for policy in ({"max_event_risk": "11"}, {"max_account_risk": "11"}, {"max_asset_inventory": "11"}):
            self.assertEqual(self.target(**policy)["state"], "SELL_ONLY")
        self.assertEqual(self.target(cash="2", inventory="0")["state"], "NO_QUOTE")

    def test_reservations_across_assets(self):
        account = {"cash": "5", "positions": {"a": "0", "b": "0"}}
        orders = [{"state": "OPEN", "asset_id": "b", "side": "BUY", "price": ".49", "remaining": "10"}]
        cat = {a: {"event_id": "e"} for a in ("a", "b")}
        self.assertEqual(targets(frame(), account, orders, cat, ".01")["state"], "NO_QUOTE")


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/"paper.sqlite"
        self.store = PaperStore(self.path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def executor(self, events, **policy):
        tape = self.store.seal(events, {"start_ms": events[0]["time_ms"], "end_ms": events[-1]["time_ms"]})
        return Executor(self.store, tape, Policy(initial_inventory="10", target_inventory="10", **policy))

    def fills(self, lane):
        return [json.loads(r[0]) for r in self.store.db.execute("SELECT payload FROM qe_journal WHERE lane=? AND kind IN ('FILLED','PARTIALLY_FILLED')", (lane,))]

    def test_touch_vs_through_and_lane_isolation(self):
        ex = self.executor([event(frame()), event(frame(AT+1000, ".48", ".49")), event(frame(AT+1500), [trade(AT+1500)])])
        ex.run(close=True)
        self.assertTrue(self.fills("1c-L0"))
        self.assertTrue(self.fills("1c-L1"))
        self.assertFalse(self.fills("3c-L1"))  # price .47 equals 3c limit; strict through only
        self.assertNotEqual(ex.account("1c-L0"), ex.account("1c-L1"))
        self.assertEqual(ex.audit()["real_submission_calls"], 0)

    def test_duplicate_trade_never_refills(self):
        t = trade(AT+1000)
        ex = self.executor([event(frame()), event(frame(AT+1000), [t]), event(frame(AT+2000), [t])])
        ex.run()
        self.assertEqual(len(self.fills("1c-L1")), 1)
        self.assertEqual(Decimal(self.fills("1c-L1")[0]["evidence"]["size"]), 2)

    def test_cancel_wins_disconnect_and_tick_change(self):
        for change in ("disconnect", "tick", "start"):
            with self.subTest(change=change):
                f = frame(AT+1000, ".48", ".49")
                if change == "disconnect":
                    f["connected"] = False
                elif change == "tick":
                    f["book"]["tick_size"] = "0.005"
                else:
                    f["market"]["game_start_ms"] = AT+1000
                ex = self.executor([event(frame()), event(f)], ttl_ms=30001 if change == "tick" else 30000 if change == "disconnect" else 30002)
                ex.run(close=True)
                rows = self.store.db.execute("SELECT payload FROM qe_journal WHERE run_id=? AND kind IN ('FILLED','PARTIALLY_FILLED')", (ex.run_id,)).fetchall()
                self.assertEqual(rows, [])

    def test_expiry_precedes_touch(self):
        ex = self.executor([event(frame()), event(frame(AT+1000, ".48", ".49"))], ttl_ms=1000)
        ex.run()
        self.assertFalse(self.fills("1c-L0"))
        self.assertTrue(self.store.db.execute("SELECT 1 FROM qe_orders WHERE state='EXPIRED'").fetchone())

    def test_restart_idempotence_and_journal_audit(self):
        ex = self.executor([event(frame()), event(frame(AT+1000), [trade(AT+1000)]), event(frame(AT+2000))])
        ex.run(max_frames=1)
        self.store.close()
        self.store = PaperStore(self.path)
        resumed = Executor(self.store, ex.tape_id, ex.policy)
        resumed.run(close=True)
        before = self.store.db.execute("SELECT count(*) FROM qe_journal").fetchone()[0]
        resumed.run(close=True)
        self.assertEqual(before, self.store.db.execute("SELECT count(*) FROM qe_journal").fetchone()[0])
        self.assertEqual(resumed.audit()["orders"], self.store.db.execute("SELECT count(*) FROM qe_orders").fetchone()[0])

    def test_transaction_rollback_preserves_cursor(self):
        ex = self.executor([event(frame()), event(frame(AT+1000))])
        original = ex.step
        def failure(row):
            original(row)
            raise RuntimeError("crash before commit")
        with patch.object(ex, "step", side_effect=failure), self.assertRaises(RuntimeError):
            ex.run()
        self.assertEqual(self.store.db.execute("SELECT cursor FROM qe_runs").fetchone()[0], 0)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM qe_orders").fetchone()[0], 0)
        ex.run()
        ex.audit()

    def test_immutable_tape_and_no_network(self):
        ex = self.executor([event(frame())])
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.db.execute("UPDATE qe_input SET time_ms=0")
        self.store.db.rollback()
        with patch("socket.socket", side_effect=AssertionError("network forbidden")), patch("httpx.Client", side_effect=AssertionError("SDK/network forbidden")):
            ex.run(close=True)
        self.assertEqual(ex.audit()["real_submission_calls"], 0)

    def test_quote_and_fill_markouts_no_future_peek(self):
        ex = self.executor([event(frame()), event(frame(AT+1000), [trade(AT+1000)]), event(frame(AT+10000)), event(frame(AT+11000)), event(frame(AT+310000))])
        ex.run(max_frames=1)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM qe_marks WHERE status='OBSERVED'").fetchone()[0], 0)
        ex.run()
        kinds = {r[0] for r in self.store.db.execute("SELECT DISTINCT kind FROM qe_marks WHERE status='OBSERVED'")}
        self.assertEqual(kinds, {"QUOTE", "HYPOTHETICAL_FILL"})
        horizons = {r[0] for r in self.store.db.execute("SELECT DISTINCT horizon_s FROM qe_marks")}
        self.assertEqual(horizons, {10, 30, 60, 300})

    def test_invalid_print_does_not_fill(self):
        for kwargs in ({"size": "0"}, {"price": "NaN"}, {"side": "UNKNOWN"}, {"price": ".49"}):
            ex = self.executor([event(frame()), event(frame(AT+1000), [trade(AT+1000, **kwargs)])])
            ex.run()
            self.assertEqual(self.store.db.execute("SELECT count(*) FROM qe_journal WHERE run_id=? AND lane LIKE '%L1' AND kind IN ('FILLED','PARTIALLY_FILLED')", (ex.run_id,)).fetchone()[0], 0)

    def test_freeze_uses_asof_evidence_and_no_valid_odds(self):
        source = Path(self.tmp.name)/"source.sqlite"
        s = FairStore(source)
        s.base.markets([market()], AT-5000)
        s.base.health(AT-5000, "connected", "test")
        observation(s, AT)
        tape = freeze(self.store, source, AT, AT+1000)
        ex = Executor(self.store, tape)
        ex.run()
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM qe_orders").fetchone()[0], 0)
        qs = [quote(updated=AT+2000), quote("b", updated=AT+2000)]
        s.ingest(OddsBatch("test", AT+2000, {"quotes": qs}, qs, "synthetic"))
        tape2 = freeze(self.store, source, AT, AT+1000)
        self.assertEqual(tape, tape2)
        s.close()

    def test_no_future_asset_inventory_seed(self):
        future = frame(AT+1000)
        future["asset_id"] = future["book"]["asset_id"] = future["signal"]["asset_id"] = "future-asset"
        ex = self.executor([event(frame()), event(future)])
        ex.run(max_frames=1)
        self.assertEqual(set(ex.account("1c-L0")["positions"]), {"a"})
        self.assertEqual(set(ex.catalog), {"a"})
        ex.run()
        self.assertIn("future-asset", ex.account("1c-L0")["positions"])

    def test_restart_after_partial_cancel_and_terminal_states(self):
        disconnected = frame(AT+2000)
        disconnected["connected"] = False
        ex = self.executor([event(frame()), event(frame(AT+1000), [trade(AT+1000)]), event(disconnected), event(frame(AT+3000), [trade(AT+3000, key="after-cancel")])])
        ex.run(max_frames=3)
        cancelled = {r[0] for r in self.store.db.execute("SELECT id FROM qe_orders WHERE state='CANCELLED'")}
        self.store.close()
        self.store = PaperStore(self.path)
        resumed = Executor(self.store, ex.tape_id, ex.policy)
        resumed.run(close=True)
        for order_id in cancelled:
            self.assertEqual(self.store.db.execute("SELECT state FROM qe_orders WHERE id=?", (order_id,)).fetchone()[0], "CANCELLED")

    def test_print_volume_capped_and_unchanged_book_not_reused(self):
        f = frame(AT+1000, ".48", ".49", "same")
        f["book"]["asks"][0]["size"] = "2"
        later = copy.deepcopy(f)
        later["time_ms"] = later["signal"]["decision_ms"] = AT+2000
        ex = self.executor([event(frame()), event(f), event(later)])
        ex.run()
        self.assertEqual(sum(Decimal(x["evidence"]["size"]) for x in self.fills("1c-L0")), 2)

    def test_reproducible_resume_equals_uninterrupted(self):
        events = [event(frame()), event(frame(AT+1000), [trade(AT+1000)]), event(frame(AT+2000)), event(frame(AT+31000))]
        ex = self.executor(events)
        ex.run(max_frames=2)
        Executor(self.store, ex.tape_id, ex.policy).run(close=True)
        other = PaperStore(Path(self.tmp.name)/"other.sqlite")
        try:
            tape = other.seal(events, {"start_ms": AT, "end_ms": AT+31000})
            run = Executor(other, tape, ex.policy)
            run.run(close=True)
            for table in ("qe_accounts", "qe_orders", "qe_decisions", "qe_marks", "qe_journal"):
                a = [tuple(r) for r in self.store.db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                b = [tuple(r) for r in other.db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                self.assertEqual(a, b, table)
        finally:
            other.close()
