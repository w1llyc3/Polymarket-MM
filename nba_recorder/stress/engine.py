"""Versioned, deterministic fault injection on the durable PAPER OMS.

Each case owns its database. Virtual time is the only clock. No network,
credentials, random outcomes, or real exchange methods are involved.
"""
import json
from pathlib import Path

from ..fair_value.demo import AT, market
from ..fair_value.maths import decimal
from ..paper_execution.manager import OrderManager
from ..paper_execution.risk import ExecutionPolicy
from ..paper_execution.store import ExecutionStore
from ..storage import encode


def fixture(at=AT, asset="a"):
    return {"time_ms": at, "market_id": "m", "asset_id": asset,
            "market": {**market(), "market_id": "m"}, "connected": True,
            "book_received_ms": at, "book_ref": str(at),
            "book": {"market_id": "m", "asset_id": asset, "bid": ".49", "ask": ".51",
                     "tick_size": ".01", "minimum_order_size": "5", "flags": [],
                     "exchange_timestamp": at, "bids": [{"price": ".49", "size": "100"}],
                     "asks": [{"price": ".51", "size": "100"}]},
            "signal": {"id": str(at), "decision_ms": at, "market_id": "m", "asset_id": asset,
                       "status": "RESEARCH_SIGNAL", "reference_eligible": True,
                       "fair_probability": ".5", "market_observed_ms": at,
                       "config": {"metadata_max_age_ms": 360000, "book_max_age_ms": 30000, "odds_max_age_ms": 120000},
                       "sources": [{"source_id": s, "included": True, "updated_ms": at, "received_ms": at} for s in ("s1", "s2")]}}


def require(condition, message):
    if not condition:
        raise AssertionError(message)


CASES = ("fair_value_jump", "odds_stale", "odds_conflict", "websocket_disconnect",
         "rest_timeout", "cancel_partial_race", "submit_response_lost", "duplicate_fill",
         "simultaneous_fills", "service_restart", "game_start_boundary", "daily_loss",
         "account_mismatch", "submission_boundary", "manual_reset_barrier", "pause")


class FaultInjectionEngine:
    version = "lesson7-v1"

    def __init__(self, directory):
        self.directory = Path(directory)

    def run_case(self, name):
        if name not in CASES:
            raise ValueError("unknown fault scenario")
        path = self.directory/(name+".sqlite")
        if path.exists():
            raise ValueError("refuse to overwrite fault evidence")
        store = ExecutionStore(path)
        policy = ExecutionPolicy(initial_inventory="10", target_inventory="10", max_daily_loss="1")
        om = OrderManager(store, policy, AT)

        def queue(key="one", asset="a"):
            return om.enqueue(key, {"side": "BUY", "price": ".49", "size": "5", "expires_ms": om.now+30000}, fixture(om.now, asset))

        try:
            order = queue()
            if name == "submission_boundary":
                om.advance(AT+30001)
                require(om.dispatch() == 0, "expired order submitted")
                require(om.adapter.authoritative(order) is None, "expired intent reached paper exchange")
            else:
                om.dispatch(drop_response=name == "submit_response_lost")
                om.advance(AT+5)
                if name in {"fair_value_jump", "odds_stale", "odds_conflict", "websocket_disconnect", "game_start_boundary"}:
                    f = fixture(om.now)
                    if name == "fair_value_jump":
                        f["signal"]["fair_probability"] = ".2"
                    elif name == "odds_stale":
                        f["signal"]["sources"][0]["updated_ms"] -= 200000
                    elif name == "odds_conflict":
                        f["signal"].update(status="NO_QUOTE", reference_eligible=False, reasons=["source_conflict"])
                    elif name == "websocket_disconnect":
                        f["connected"] = False
                    else:
                        f["market"]["game_start_ms"] = om.now+policy.start_guard_ms
                    require(bool(om.risk_check(f)), "fault failed to trigger risk")
                    require(om.halted, "risk fault did not block submissions")
                elif name == "rest_timeout":
                    om.kill("rest_test")
                    require(not om.reconcile(latency_ms=policy.rest_timeout_ms+1), "REST timeout treated as success")
                    require(om.risk_state == "HALTED", "REST failure unblocked risk")
                elif name == "cancel_partial_race":
                    om.cancel(order)
                    om.fill(order, "2", "race")
                    om.advance(om.now)
                    require(om.order(order)["state"] == "CANCEL_PENDING", "partial fill erased pending cancel")
                    require(decimal(om.account()["positions"]["a"]) == 12, "partial inventory missing")
                    om.advance(om.now+10)
                    require(om.order(order)["state"] == "CANCELLED", "cancel failed")
                elif name == "submit_response_lost":
                    om.advance(AT+100)
                    require(om.order(order)["state"] == "UNKNOWN", "lost response did not become UNKNOWN")
                    require(om.adapter.authoritative(order) is not None, "acceptance not simulated")
                    require(not om.resume(), "UNKNOWN auto-unlocked")
                elif name == "duplicate_fill":
                    require(om.fill(order, "2", "duplicate"), "first fill missing")
                    require(not om.fill(order, "2", "duplicate"), "duplicate accepted")
                    om.advance(om.now)
                    require(decimal(om.account()["positions"]["a"]) == 12, "duplicate inventory")
                elif name == "simultaneous_fills":
                    second = queue("two", "b")
                    om.dispatch()
                    om.advance(om.now+5)
                    om.fill(order, "5", "a-full")
                    om.fill(second, "5", "b-full")
                    om.advance(om.now)
                    require(all(decimal(q) == 15 for q in om.account()["positions"].values()), "simultaneous inventory wrong")
                    require(decimal(om.account()["cash"]) == decimal(policy.initial_cash)-decimal("4.90"), "cash wrong")
                elif name == "service_restart":
                    om.fill(order, "2", "unreceived", transport_ms=30)
                    store.close()
                    store = ExecutionStore(path)
                    om = OrderManager(store, policy)
                    require(om.risk_state == "HALTED", "restart unblocked")
                    require(om.order(order)["state"] == "UNKNOWN", "restart trusted cached order")
                    om.advance(om.now+40)
                    require(om.reconcile(), "restart reconciliation failed")
                    require(decimal(om.account()["positions"]["a"]) == 12, "restart inventory lost")
                    require(om.halted, "restart auto resumed")
                elif name == "daily_loss":
                    om.mark_equity([fixture(om.now)])
                    f = fixture(om.now)
                    f["book"].update(bid=".29", ask=".31")
                    om.mark_equity([f])
                    require(om.risk_state == "HALTED", "daily breaker missing")
                    om.advance(om.now+10)
                    om.reconcile()
                    require(not om.resume(), "daily breaker reset allowed")
                elif name == "account_mismatch":
                    with store.db:
                        # Authority fill ledger is inconsistent even when OMS/order match.
                        store.db.execute("INSERT INTO pe_fills VALUES('unreported',?,?,?)", (order, om.now, "1"))
                    require(not om.reconcile(), "independent fill ledger mismatch hidden")
                    require(om.risk_state == "HALTED", "mismatch unblocked")
                elif name == "manual_reset_barrier":
                    om.kill("severe_breaker")
                    require(not om.resume(), "reset before reconciliation allowed")
                    om.advance(om.now+10)
                    require(om.reconcile(), "cancel reconciliation failed")
                    require(om.risk_state == "READY_FOR_MANUAL_RESET", "ready state missing")
                    require(om.dispatch() == 0, "ready state submitted")
                    require(om.resume(), "manual reset failed")
                elif name == "pause":
                    require(om.pause(), "pause failed")
                    require(om.risk_state == "PAUSED", "pause state missing")
                    require(om.dispatch() == 0, "paused submission")
            if om.halted:
                blocked = queue("blocked")
                require(om.order(blocked)["state"] == "REJECTED", "blocked risk intent accepted")
                require(om.dispatch() == 0, "blocked exchange submission")
            require(om.real_submission_calls == om.adapter.real_submission_calls == 0, "live submission")
            return {"scenario": name, "status": "PASS", "risk_state": om.risk_state,
                    "orders": [{"state": o["state"], "filled": o["filled"]} for o in om.orders()],
                    "risk_reasons": [r[0] for r in store.db.execute("SELECT reason FROM pe_risks ORDER BY id")],
                    "latency_ms": om.status()["latency_ms"], "real_submission_calls": 0}
        finally:
            store.close()

    def run(self):
        results = []
        for name in CASES:
            try:
                results.append(self.run_case(name))
            except Exception as error:
                results.append({"scenario": name, "status": "FAIL", "error": type(error).__name__+": "+str(error)})
        return {"version": self.version, "provenance": "synthetic_paper_clock",
                "passed": sum(r["status"] == "PASS" for r in results),
                "failed": sum(r["status"] == "FAIL" for r in results), "results": results,
                "authority_scope": "independent reducer, same SQLite/process; not independent service"}
