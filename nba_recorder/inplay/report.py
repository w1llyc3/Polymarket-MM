import json
from ..fair_value.engine import table_exists
from ..fair_value.maths import decimal
from ..fill_management.ledger import valuation
from .reference import book_risk


def execution_evidence(om):
    def count(kind):
        return om.db.execute("SELECT count(*) FROM pe_journal WHERE kind=?", (kind,)).fetchone()[0]
    orders = om.orders()
    receipts = [json.loads(r[0]) for r in om.db.execute("SELECT payload FROM pe_journal WHERE kind IN ('EXECUTION_REPORT','RECONCILED')")]
    accepted_ids, cancelled_ids = set(), set()
    for receipt in receipts:
        authority = (receipt.get("detail") or {}).get("authority") or {}
        if authority.get("state") in {"OPEN", "PARTIALLY_FILLED", "FILLED", "CANCELLED"}:
            accepted_ids.add(authority["id"])
        if authority.get("state") == "CANCELLED":
            cancelled_ids.add(authority["id"])
    accepted = len(accepted_ids)
    fills = om.db.execute("SELECT count(*) FROM ip_receipts").fetchone()[0]
    cancelled = len(cancelled_ids)
    decisions = [json.loads(r[0]) for r in om.db.execute("SELECT payload FROM pe_quotes")]
    inputs = om.db.execute("SELECT count(*) FROM ip_inputs").fetchone()[0]
    reduced = any(o.get("purpose") == "RISK_EXIT" and decimal(o["filled"]) > 0 for o in orders)
    return {"input_loop_operational": inputs > 0,
            "eligible_quote_decisions": sum(bool(d.get("targets")) for d in decisions),
            "intents_created": len(orders), "local_rejections": count("LOCAL_REJECTION")+count("DISPATCH_RISK_REJECTED"),
            "paper_submissions": count("SUBMIT_REQUEST"), "paper_orders_accepted": accepted,
            "simulated_fill_events": fills, "cancel_acknowledged": cancelled,
            "inventory_reconciled": om.consistent(), "reduce_exit_observed": reduced,
            "full_game_coverage_verified": False,
            "quote_execution_acceptance_status": "NOT_VERIFIED",
            "evidence_refs": ["pe_journal", "pe_quotes", "pe_exchange", "pe_fills", "ip_inputs", "ip_receipts"],
            "limitation": "component observations never automatically certify complete real-game execution acceptance"}


def report(manager):
    om = manager
    def diagnostics(table):
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in om.db.execute(f"SELECT * FROM {table} ORDER BY id")] if table_exists(om.db, table) else []
    inputs = [json.loads(r[0]) for r in om.db.execute("SELECT payload FROM ip_inputs ORDER BY time_ms,rowid")]
    frames = [f for e in inputs for f in e["frames"]]
    marks = {}
    for f in frames:
        # Final marks only from fresh books with consistent identities; phase
        # gate affects quoting, not whether the last valid mid was observed.
        candidate = {**f, "phase": "ACTIVE_MM"}
        permission = {"late_close_threshold", "late_reduce_boundary", "phase_not_permitted", "unreliable_sports_state", "sports_stale_or_future", "invalid_clock"}
        if not set(book_risk(candidate, om.policy, om.now))-permission:
            marks[f["asset_id"]] = str((decimal(f["book"]["bid"])+decimal(f["book"]["ask"]))/2)
    state = om.ledger.state()
    account = valuation(state, marks, om.orders())
    decisions = [json.loads(r[0]) for r in om.db.execute("SELECT payload FROM pe_quotes ORDER BY time_ms,rowid")]
    fills = [dict(r) for r in om.db.execute("SELECT * FROM l6_events WHERE kind='FILL' AND run_id=? ORDER BY seq", (om.run_id,)) if json.loads(r["payload"]).get("purpose") != "SIMULATED_COMPLETE_SET_SPLIT"]
    markouts = []
    for row in fills:
        fill = json.loads(row["payload"])
        exchange_ms = fill.get("context", {}).get("exchange_ms", row["time_ms"])
        # Retrospective diagnostic anchored to virtual exchange execution.
        # Receipt time is retained; neither time creates a historical signal.
        at = exchange_ms
        for horizon in (1, 3, 5, 10, 30):
            due = at+horizon*1000
            future = next((f for f in frames if f["asset_id"] == fill["asset_id"] and due <= f["time_ms"] <= due+1000), None)
            value, status = None, "RIGHT_CENSORED" if not inputs or due > inputs[-1]["time_ms"] else "UNAVAILABLE"
            if future:
                candidate = {**future, "phase": "ACTIVE_MM"}
                if not set(book_risk(candidate, om.policy, future["time_ms"] ))-{"late_close_threshold", "late_reduce_boundary", "phase_not_permitted"}:
                    mid = (decimal(future["book"]["bid"])+decimal(future["book"]["ask"]))/2
                    value = str((mid-decimal(fill["price"]))* (1 if fill["side"] == "BUY" else -1))
                    status = "OBSERVED"
            markouts.append({"fill_id": row["id"], "horizon_s": horizon, "signed_markout_per_share": value, "adverse": decimal(value) < 0 if value is not None else None, "status": status, "receipt_ms": row["time_ms"], "exchange_ms": exchange_ms, "fill_mid": fill.get("context", {}).get("reference_mid"), "diagnostic_not_added_to_pnl": True})
    incomes = [dict(r) for r in om.db.execute("SELECT * FROM ip_income ORDER BY time_ms,rowid")]
    totals = {}
    for row in incomes:
        if row["kind"] == "SPLIT":
            continue
        key = row["kind"]+":"+row["stage"]+(":unallocated" if row["market_id"] is None else ":allocated")
        totals[key] = str(decimal(totals.get(key, "0"))+decimal(row["amount"]))
    orders = om.orders()
    filled = [o for o in orders if decimal(o["filled"]) > 0]
    residual = {a: q for a, q in account["positions"].items() if decimal(q) > 0}
    ages = [{"lot_id": l["id"], "asset_id": l["asset_id"], "remaining": l["remaining"], "age_ms": om.now-(l.get("context", {}).get("exchange_ms") or l["time_ms"]), "local_observed_age_ms": om.now-l["time_ms"]} for l in state["lots"] if decimal(l["remaining"]) > 0]
    costs = sum((decimal(r["amount"]) for r in incomes if r["kind"] in {"SIMULATED_COST", "RUNNING_COST"} and r["stage"] == "RECEIVED"), decimal(0))
    stale_lots = [l for l in state["lots"] if l.get("context", {}).get("stale_quote")]
    stale_closures = [c for c in state["closures"] if any(l["id"] == c["lot_id"] for l in stale_lots)]
    stale_loss = sum((min(decimal(0), decimal(c["gross_pnl"])) for c in stale_closures if c["gross_pnl"] is not None), decimal(0)) if stale_closures else None
    return {"mode": "BOOK_DRIVEN_UNIFIED_PAPER", "strategy": "B" if om.policy.enhanced else "A", "policy": __import__('dataclasses').asdict(om.policy),
            "risk_state": om.risk_state, "phases": [json.loads(r[0]) for r in om.db.execute("SELECT payload FROM ip_phase")],
            "account": account, "income_stages": totals, "income_allocation_unknown": any(r["market_id"] is None for r in incomes if r["kind"] in {"MAKER_REBATE", "REWARD"}),
            "pnl_layers": {"trading_pnl": account["trading_pnl"], "execution_fees": str(decimal(state["fees"])-costs), "recorded_running_costs": str(costs), "received_income": state["rewards"], "net_pnl": account["net_pnl"], "expected_or_confirmed_rebates_in_cash": False, "income_basis": "explicit paper evidence only; unallocated remains unallocated"},
            "realized_book_attribution": [c["attribution"].get("book_decomposition") for c in state["closures"]],
            "stale_quote_loss_realized_diagnostic": str(stale_loss) if stale_loss is not None else None,
            "overlapping_diagnostics_not_added_to_pnl": True,
            "quote_availability": str(decimal(sum(d["state"] != "NO_QUOTE" for d in decisions))/len(decisions)) if decisions else None,
            "orders": len(orders), "fill_events": len(fills), "simulated_fill_order_rate": str(decimal(len(filled))/len(orders)) if orders else None,
            "quote_distance": om.policy.quote_distance,
            "inventory_rule": "SUPPRESS_BUY_ON_NET_LONG; ask distance floor unchanged",
            "public_flow_diagnostics": diagnostics("ip_flow_diagnostics"),
            "own_fill_diagnostics": diagnostics("ip_own_fills"),
            "flow_risk_policy": "explicit optional PAUSE" if om.policy.public_flow_pause else "public volume diagnostic only; account hard limits unchanged",
            "phase_authority": "MANUAL_CONFIRMED_NOT_UNATTENDED" if om.policy.allow_manual_phase else "AUTOMATIC_CONTRACT_REQUIRED",
            "experimental_boundaries_s": {"reduce": om.policy.reduce_remaining_s, "close": om.policy.close_remaining_s, "calibrated_or_user_approved": False},
            "outcome_edge": "NOT_COMPUTED: book distance is not outcome fair value edge", "markouts": markouts,
            "inventory_ages": ages, "residual_inventory": residual, "residual_risk_status": "MANUAL_REVIEW" if residual else "FLAT",
            "ledger_and_oms_consistent": om.consistent(), "latency_ms": om.status()["latency_ms"], "latency_provenance": "synthetic_paper_clock",
            "risk_reasons": [dict(r) for r in om.db.execute("SELECT reason,count(*) AS count FROM pe_risks GROUP BY reason ORDER BY reason")],
            "coverage": {"inputs": len(inputs), "asset_frames": len(frames), "markets": len({f["market_id"] for f in frames}), "first_ms": inputs[0]["time_ms"] if inputs else None, "last_ms": inputs[-1]["time_ms"] if inputs else None},
            "statistical_unit": "game, not correlated frames", "confidence": "uncalibrated; fixtures do not validate profitability or execution probability",
            "execution_evidence": execution_evidence(om), "live_go": False, "real_submission_calls": 0}
