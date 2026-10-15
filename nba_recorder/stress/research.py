"""Read-only projection of nine counterfactual lanes; no live claims."""
import json
from datetime import datetime, timezone
from ..fair_value.maths import decimal
from ..fair_value.store import read_connection
from ..fill_management.report import daily_report, report_days, ratio


def summarize(path, run_id):
    db = read_connection(path)
    try:
        reports = [daily_report(db, run_id, day) for day in report_days(db, run_id)]
        if not reports:
            raise ValueError("unknown or empty research run")
        for report in reports:
            start = int(datetime.fromisoformat(report["day"]).replace(tzinfo=timezone.utc).timestamp()*1000)
            end = start+86400000-1
            for lane in report["lanes"]:
                decisions = [json.loads(r[0]) for r in db.execute("SELECT payload FROM qe_decisions WHERE run_id=? AND lane=? AND time_ms BETWEEN ? AND ?", (run_id, lane["lane"], start, end))]
                targets = [t for d in decisions for t in d.get("targets", [])]
                edges = [decimal(t["gross_edge"]) for t in targets]
                fills = [json.loads(r[0]) for r in db.execute("SELECT payload FROM l6_events WHERE run_id=? AND lane=? AND kind='FILL' AND time_ms BETWEEN ? AND ?", (run_id, lane["lane"], start, end))]
                hedges = [f for f in fills if f.get("purpose") == "HEDGE"]
                lane["acceptance_metrics"] = {
                    "quote_availability": ratio(lane["quote_opportunities"], lane["decisions"]),
                    "gross_edge_target_sample_count": len(edges),
                    "gross_edge_min": str(min(edges)) if edges else None,
                    "gross_edge_max": str(max(edges)) if edges else None,
                    "gross_edge_mean": str(sum(edges, decimal(0))/len(edges)) if edges else None,
                    "gross_edge_scope": "UTC-day target quotes, theoretical per-share; not profit",
                    "hedge_cost": str(sum((decimal(f["price"])*decimal(f["quantity"])+decimal(f.get("fee", "0")) for f in hedges), decimal(0))) if hedges else None,
                    "hedge_cost_sample_count": len(hedges),
                    "cancel_latency_ms": None, "reconciliation_latency_ms": None,
                    "latency_scope": "instant-cancel research replay; use separate OMS fault latency, never join as measured live latency",
                    "stale_quote_loss": lane["attribution"]["stale_quote_loss"]}
        return {"reports": reports, "sample_provenance": "caller must identify source; demo is synthetic",
                "live_go": False, "real_submission_calls": 0}
    finally:
        db.close()
