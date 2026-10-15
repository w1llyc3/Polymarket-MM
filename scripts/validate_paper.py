"""Read-only artifact checks and compact reproducible validation output."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nba_recorder.fair_value.store import read_connection
from nba_recorder.quote_engine.storage import PaperStore
from nba_recorder.quote_engine.executor import Executor
from nba_recorder.paper_execution.adapter import PaperExchangeAdapter
from nba_recorder.paper_execution.manager import OrderManager


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--quotes", required=True)
    p.add_argument("--execution", required=True)
    args = p.parse_args()
    store = PaperStore.readonly(args.quotes)
    execution = read_connection(args.execution)
    try:
        quote_runs = []
        for r in store.db.execute("SELECT * FROM qe_runs"):
            manifest = store.verify(r["tape_id"])
            auditor = Executor.__new__(Executor)
            auditor.db, auditor.run_id = store.db, r["id"]
            audit = auditor.audit()
            states = [dict(x) for x in store.db.execute("SELECT lane,state,count(*) AS count FROM qe_orders WHERE run_id=? GROUP BY lane,state", (r["id"],))]
            marks = [dict(x) for x in store.db.execute("SELECT kind,status,horizon_s,count(*) AS count FROM qe_marks WHERE run_id=? GROUP BY kind,status,horizon_s", (r["id"],))]
            decisions = store.db.execute("SELECT count(*) FROM qe_decisions WHERE run_id=?", (r["id"],)).fetchone()[0]
            quote_runs.append({**audit, "cursor": r["cursor"], "state": r["state"], "tape_verified": True, "frame_count": manifest["count"], "decisions": decisions, "states": states, "markouts": marks})
        manager = OrderManager.__new__(OrderManager)
        manager.db = execution
        manager.adapter = PaperExchangeAdapter(execution)
        control = dict(execution.execute("SELECT * FROM pe_control").fetchone())
        result = {"quote_runs": quote_runs, "paper_execution": {"mode": control["mode"], "state": control["state"], "clock_ms": control["clock_ms"], "authority_consistent": manager.consistent(),
                  "orders": execution.execute("SELECT count(*) FROM pe_orders").fetchone()[0], "fill_events": execution.execute("SELECT count(*) FROM pe_fills").fetchone()[0],
                  "journal_events": execution.execute("SELECT count(*) FROM pe_journal").fetchone()[0], "risk_events": execution.execute("SELECT count(*) FROM pe_risks").fetchone()[0],
                  "latency_ms": OrderManager.latency_summary(execution), "latency_provenance": "synthetic_paper_clock", "real_submission_calls": 0}, "not_live_performance": True}
        print(json.dumps(result, indent=2))
    finally:
        store.close()
        execution.close()


if __name__ == "__main__":
    main()
