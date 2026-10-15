"""UTC-day paper research output; overlapping attribution is never added to PnL."""
import json
from collections import Counter
from datetime import datetime, timezone
from decimal import localcontext

from ..fair_value.maths import PRECISION, decimal
from ..quote_engine.model import hard_risk, Policy
from ..storage import encode
from .ledger import Ledger, balance, valuation


def ratio(a, b):
    return str(decimal(a)/decimal(b)) if b else None


def daily_report(db, run_id, day):
    with localcontext() as ctx:
        ctx.prec = PRECISION
        return _daily_report(db, run_id, day)


def _daily_report(db, run_id, day):
    start = int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp()*1000)
    end = start+86400000-1
    run = db.execute("SELECT * FROM qe_runs WHERE id=?", (run_id,)).fetchone()
    if not run:
        raise ValueError("unknown research run")
    policy = Policy(**json.loads(run["config"]))
    config = json.loads(db.execute("SELECT payload FROM l6_config WHERE run_id=?", (run_id,)).fetchone()[0])
    processed = db.execute("SELECT max(time_ms) FROM qe_input WHERE tape_id=? AND seq<=?", (run["tape_id"], run["cursor"])).fetchone()[0]
    markout_cutoff = processed if processed is not None else start-1
    inputs = [json.loads(r[0]) for r in db.execute("SELECT payload FROM qe_input WHERE tape_id=? AND seq<=? AND time_ms BETWEEN ? AND ? ORDER BY seq", (run["tape_id"], run["cursor"], start, end))]
    frames = [f for e in inputs for f in e["frames"]]
    valid = sum(not hard_risk(f, policy) for f in frames)
    anomalies = Counter(reason for f in frames for reason in hard_risk(f, policy))
    lanes = []
    for lane, in db.execute("SELECT lane FROM l6_accounts WHERE run_id=? ORDER BY lane", (run_id,)):
        ledger = Ledger(db, run_id, lane)
        state = ledger.rebuild(end)
        prior = ledger.rebuild(start-1)
        snaps = [json.loads(r[0]) for r in db.execute("SELECT payload FROM l6_valuations WHERE run_id=? AND lane=? AND time_ms BETWEEN ? AND ? ORDER BY time_ms,input_seq", (run_id, lane, start, end))]
        if not snaps:
            continue
        closing = snaps[-1]
        previous = db.execute("SELECT time_ms,payload FROM l6_valuations WHERE run_id=? AND lane=? AND time_ms<? ORDER BY time_ms DESC,input_seq DESC LIMIT 1", (run_id, lane, start)).fetchone()
        prior_held = any(decimal(l["remaining"]) > 0 for l in prior["lots"])
        if not prior_held:
            opening = valuation(prior, {})
        elif previous and 0 <= start-previous["time_ms"] <= policy.markout_window_ms:
            opening = json.loads(previous["payload"])
        else:
            opening = {"equity": None, "net_pnl": None, "trading_pnl": None}
        if closing["positions"] != {a: str(balance(state, a)) for a in state["assets"]} or decimal(closing["cash"]) != decimal(state["cash"]):
            raise ValueError("daily closing valuation does not match as-of accounting events")
        net_day = decimal(closing["equity"])-decimal(opening["equity"])-(decimal(state["capital"])-decimal(prior["capital"])) if closing["net_pnl"] is not None and opening["net_pnl"] is not None else None
        trading_day = decimal(closing["trading_pnl"])-decimal(opening["trading_pnl"]) if closing["trading_pnl"] is not None and opening["trading_pnl"] is not None else None
        fees, rewards = decimal(state["fees"])-decimal(prior["fees"]), decimal(state["rewards"])-decimal(prior["rewards"])
        if net_day is not None and net_day != trading_day-fees+rewards:
            raise ValueError("daily Net PnL reconciliation failed")
        decisions = [json.loads(r[0]) for r in db.execute("SELECT payload FROM qe_decisions WHERE run_id=? AND lane=? AND time_ms BETWEEN ? AND ?", (run_id, lane, start, end))]
        # Journal states as-of the reporting cutoff, not today's mutable final order state.
        orders = {}
        for r in db.execute("SELECT payload FROM qe_journal WHERE run_id=? AND lane=? AND time_ms<=? ORDER BY id", (run_id, lane, end)):
            p = json.loads(r[0])
            if p.get("order"):
                orders[p["order"]["id"]] = p["order"]
        day_orders = [o for o in orders.values() if start <= o["created_ms"] <= end]
        filled = [o for o in day_orders if decimal(o["size"])-decimal(o["remaining"]) > 0]
        partial = [o for o in filled if decimal(o["remaining"]) > 0]
        quantity = sum((decimal(o["size"]) for o in day_orders), decimal(0))
        filled_qty = sum((decimal(o["size"])-decimal(o["remaining"]) for o in day_orders), decimal(0))
        cancel_qty = sum((decimal(o["remaining"]) for o in day_orders if o["state"] in {"CANCELLED", "EXPIRED"}), decimal(0))
        pending_qty = sum((decimal(o["remaining"]) for o in day_orders if o["state"] not in {"CANCELLED", "EXPIRED", "FILLED"}), decimal(0))
        if quantity != filled_qty+cancel_qty+pending_qty:
            raise ValueError("order quantity conservation failed")
        fills = [dict(r) for r in db.execute("SELECT * FROM l6_events WHERE run_id=? AND lane=? AND kind='FILL' AND time_ms BETWEEN ? AND ?", (run_id, lane, start, end))]
        fill_payloads = [json.loads(r["payload"]) for r in fills]
        marks = {}
        for horizon in (10, 30, 60, 300):
            samples, weight, statuses = decimal(0), decimal(0), Counter()
            for fill in fill_payloads:
                row = db.execute("SELECT * FROM qe_marks WHERE anchor_id=? AND kind='HYPOTHETICAL_FILL' AND horizon_s=?", (fill.get("markout_anchor"), horizon)).fetchone()
                if not row:
                    statuses["NO_MARKOUT_SOURCE"] += 1
                    continue
                p = json.loads(row["payload"])
                status = row["status"] if p.get("evaluated_ms", markout_cutoff+1) <= markout_cutoff else "PENDING_ASOF_CURSOR"
                statuses[status] += 1
                if status == "OBSERVED":
                    qty = decimal(fill["quantity"])
                    samples += decimal(p["markout"])*qty
                    weight += qty
            marks[str(horizon)] = {"quantity_weighted_per_share": str(samples/weight) if weight else None, "statuses": dict(statuses), "diagnostic_not_pnl": True}
        markets = {}
        for asset, meta in state["assets"].items():
            markets.setdefault(meta["market_id"], {})[meta["role"]] = {"asset_id": asset, "label": meta["label"], "quantity": str(balance(state, asset))}
        for market in markets.values():
            yes = decimal(market.get("YES", {}).get("quantity", "0"))
            no = decimal(market.get("NO", {}).get("quantity", "0"))
            market.update(paired_quantity=str(min(yes, no)), directional_quantity=str(abs(yes-no)), binary_role_basis="native YES/NO labels when present; otherwise outcome_index, with actual labels/assets retained")
        closures = [json.loads(r[0]) for r in db.execute("SELECT payload FROM l6_closures WHERE run_id=? AND lane=? AND time_ms BETWEEN ? AND ?", (run_id, lane, start, end))]
        attribution = {k: str(sum((decimal(c["attribution"][k]) for c in closures if c["attribution"].get(k) is not None), decimal(0))) for k in ("entry_edge", "spread_capture", "directional_residual", "stale_quote_loss")}
        attribution.update(covered_closures=sum(c["attribution"]["additive_reconciliation"] for c in closures), total_closures=len(closures),
                           additive_components=["entry_edge", "spread_capture", "directional_residual"], nonadditive_diagnostics=["markout", "hedge_slippage", "stale_quote_loss"],
                           hedge_slippage=None, stale_quote_loss_is_partial_realized_diagnostic=True, overlapping_metrics_never_summed=True)
        entry_samples = [p for p in fill_payloads if p["side"] == "BUY" and p.get("context", {}).get("fair_probability") is not None]
        attribution["entry_edge_at_fill_diagnostic"] = str(sum((decimal(p["quantity"])*(decimal(p["context"]["fair_probability"])-decimal(p["price"])) for p in entry_samples), decimal(0))) if entry_samples else None
        attribution["entry_edge_sample_count"] = len(entry_samples)
        hedge_fills = [p for p in fill_payloads if p.get("purpose") == "HEDGE"]
        slippage_samples = [p for p in hedge_fills if p.get("context", {}).get("expected_hedge_price") is not None]
        attribution["hedge_slippage"] = str(sum((decimal(p["quantity"])*(decimal(p["price"])-decimal(p["context"]["expected_hedge_price"])) for p in slippage_samples), decimal(0))) if slippage_samples else None
        attribution["hedge_slippage_samples"] = len(slippage_samples)
        stale_lots = [l for l in state["lots"] if decimal(l["remaining"]) > 0 and l.get("context", {}).get("stale_quote")]
        stale_closed = [c for c in closures if next((l for l in state["lots"] if l["id"] == c["lot_id"]), {}).get("context", {}).get("stale_quote")]
        stale_missing = any(l["asset_id"] not in closing.get("marks", {}) or l["price"] is None for l in stale_lots)
        if stale_lots or stale_closed:
            loss = sum((decimal(c["attribution"]["stale_quote_loss"]) for c in stale_closed), decimal(0))
            loss += sum((min(decimal(0), (decimal(closing["marks"][l["asset_id"]])-decimal(l["price"]))*decimal(l["remaining"])) for l in stale_lots), decimal(0)) if not stale_missing else decimal(0)
            attribution["stale_quote_loss"] = str(loss) if not stale_missing else None
        else:
            attribution["stale_quote_loss"] = None
        attribution.update(stale_quote_open_lots=len(stale_lots), stale_quote_closed_lots=len(stale_closed), stale_quote_loss_is_partial_realized_diagnostic=False)
        intents = [h for h in state["hedge_intents"].values() if start <= h["time_ms"] <= end]
        requested = sum((decimal(h["quantity"]) for h in intents), decimal(0))
        hedge_quantity = sum((decimal(h["filled"]) for h in intents), decimal(0))
        lanes.append({"lane": lane, "model": lane.split("-")[-1], "quote_opportunities": sum(d["state"] != "NO_QUOTE" for d in decisions), "decisions": len(decisions),
                      "orders_created": len(day_orders), "fill_events": len(fills), "submitted_quantity": str(quantity), "filled_quantity": str(filled_qty),
                      "pending_quantity": str(pending_qty), "cancelled_or_expired_quantity": str(cancel_qty), "order_fill_rate": ratio(len(filled), len(day_orders)),
                      "quantity_fill_rate": ratio(filled_qty, quantity), "partial_order_rate_among_filled": ratio(len(partial), len(filled)), "markouts": marks,
                      "hedge_success_rate": ratio(sum(decimal(h["filled"]) == decimal(h["quantity"]) for h in intents), len(intents)),
                      "hedge_quantity_completion_rate": ratio(hedge_quantity, requested), "hedge_partial_intent_rate": ratio(sum(0 < decimal(h["filled"]) < decimal(h["quantity"]) for h in intents), len(intents)),
                      "hedge_sample_count": len(intents), "hedge_fill_events_today": len(hedge_fills), "hedge_rate_basis": "same-day paper intent cohort; success requires full intended quantity, not partial fills or candidates",
                      "inventory": markets, "opening_equity": opening["equity"], "closing_account": closing,
                      "daily_trading_pnl": str(trading_day) if trading_day is not None else None, "daily_fees": str(fees), "daily_rewards": str(rewards),
                      "daily_net_pnl": str(net_day) if net_day is not None else None, "net_pnl_reconciled": net_day is not None,
                      "max_conservative_committed_exposure": str(max((decimal(s.get("exposure_before_close", s["conservative_committed_exposure"])) for s in snaps), default=decimal(0))),
                      "attribution": attribution, "valuation_missing_count": sum(not s["reconciled"] for s in snaps), "not_real_profit": True})
    return {"day": day, "timezone": "UTC", "run_id": run_id, "cursor": run["cursor"], "mode": "RECORD_ONLY_PAPER", "real_submission_calls": 0,
            "source_tape_id": run["tape_id"], "markout_evaluation_cutoff_ms": markout_cutoff, "markouts_are_retrospective_cohort_diagnostics_not_pnl": True,
            "coverage": {"time_buckets": len(inputs), "asset_frames": len(frames), "valid_reference_and_book_frames": valid,
            "valid_fraction": ratio(valid, len(frames)), "first_ms": inputs[0]["time_ms"] if inputs else None, "last_ms": inputs[-1]["time_ms"] if inputs else None,
            "distinct_book_observations": len({f.get("book_ref") for f in frames if f.get("book_ref")}), "trade_prints": sum(len(e["trades"]) for e in inputs),
            "anomalies": dict(anomalies), "coverage_is_recorded_interval_not_24h": True}, "assumptions": config, "lanes": lanes,
            "comparison": "independent counterfactual lanes; do not add account equities or PnL across models", "not_live_performance": True}


def report_days(db, run_id):
    times = [r[0] for r in db.execute("SELECT DISTINCT time_ms FROM l6_valuations WHERE run_id=?", (run_id,))]
    return sorted({datetime.fromtimestamp(t/1000, timezone.utc).date().isoformat() for t in times})


def persist_reports(db, run_id):
    reports = [daily_report(db, run_id, day) for day in report_days(db, run_id)]
    with db:
        for report in reports:
            for lane in report["lanes"]:
                db.execute("INSERT INTO l6_daily VALUES(?,?,?,?) ON CONFLICT(run_id,lane,day) DO UPDATE SET payload=excluded.payload", (run_id, lane["lane"], report["day"], encode({**lane, "coverage": report["coverage"], "assumptions": report["assumptions"]})))
    return reports
