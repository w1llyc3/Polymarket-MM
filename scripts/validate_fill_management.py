"""Read-only Lesson 6 verification; no exchange/wallet APIs."""
import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nba_recorder.quote_engine.storage import PaperStore
from nba_recorder.fill_management.engine import ResearchExecutor
from nba_recorder.fill_management.report import daily_report, report_days
from nba_recorder.fill_management.models import order_quantities


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", required=True)
    p.add_argument("--run", required=True)
    args = p.parse_args()
    store = PaperStore.readonly(args.db)
    try:
        row = store.db.execute("SELECT * FROM qe_runs WHERE id=?", (args.run,)).fetchone()
        if not row:
            raise ValueError("unknown run")
        manifest = store.verify(row["tape_id"])
        engine = ResearchExecutor.__new__(ResearchExecutor)
        engine.db, engine.run_id, engine._ready = store.db, args.run, True
        audit = engine.audit()
        for r in store.db.execute("SELECT id,payload FROM qe_orders WHERE run_id=?", (args.run,)):
            expected = order_quantities(json.loads(r["payload"]))
            cache = store.db.execute("SELECT * FROM l6_order_status WHERE id=?", (r["id"],)).fetchone()
            if not cache or any(Decimal(cache[k]) != Decimal(v) for k, v in expected.items()):
                raise ValueError("order quantities do not match journal-derived state")
        reports = [daily_report(store.db, args.run, day) for day in report_days(store.db, args.run)]
        summaries = []
        for report in reports:
            lanes = []
            for lane in report["lanes"]:
                close = lane["closing_account"]
                if close["net_pnl"] is not None and Decimal(close["cash"])+Decimal(close["market_value"])-Decimal(close["capital_contributions"]) != Decimal(close["net_pnl"]):
                    raise ValueError("underlying cash/valuation does not reconcile")
                lanes.append({k: lane[k] for k in ("lane", "quote_opportunities", "orders_created", "fill_events", "filled_quantity", "order_fill_rate", "partial_order_rate_among_filled", "daily_net_pnl", "hedge_sample_count", "hedge_success_rate", "max_conservative_committed_exposure", "net_pnl_reconciled")})
            summaries.append({"day": report["day"], "coverage": report["coverage"], "lanes": lanes})
        print(json.dumps({**audit, "tape_hash_verified": True, "frame_count": manifest["count"], "order_quantity_conservation": True,
              "candidate_statuses": [dict(r) for r in store.db.execute("SELECT json_extract(payload,'$.status') AS status,count(*) AS count FROM l6_candidates WHERE run_id=? GROUP BY 1", (args.run,))],
              "markout_statuses": [dict(r) for r in store.db.execute("SELECT kind,status,count(*) AS count FROM qe_marks WHERE run_id=? GROUP BY kind,status", (args.run,))],
              "reports": summaries, "not_live_performance": True}, indent=2))
    finally:
        store.close()


if __name__ == "__main__":
    main()
