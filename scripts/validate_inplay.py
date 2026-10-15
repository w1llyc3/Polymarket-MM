"""Read-only unified Paper audit/report; never opens a writer or resets risk."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nba_recorder.fair_value.store import read_connection
from nba_recorder.fill_management.ledger import Ledger
from nba_recorder.inplay.manager import UnifiedPaperManager
from nba_recorder.inplay.phase import PhaseController
from nba_recorder.inplay.policy import BookPolicy
from nba_recorder.inplay.report import report
from nba_recorder.paper_execution.adapter import PaperExchangeAdapter


def audit(path):
    db = read_connection(path)
    try:
        control = db.execute("SELECT * FROM pe_control WHERE id=1").fetchone()
        if not control or control["mode"] != "PAPER":
            raise ValueError("Paper control required")
        om = UnifiedPaperManager.__new__(UnifiedPaperManager)
        om.db, om.adapter = db, PaperExchangeAdapter(db)
        om.policy = BookPolicy(**json.loads(control["config"]))
        from nba_recorder.fair_value.store import digest
        from dataclasses import asdict
        om.run_id = digest(["book-driven-unified-v2", asdict(om.policy)])
        om.ledger = Ledger(db, om.run_id, "UNIFIED_PAPER")
        om.phases = PhaseController(db, om.policy)
        if not om.consistent():
            raise ValueError("unified authority/OMS/FIFO mismatch")
        result = report(om)
        result["sqlite_integrity"] = db.execute("PRAGMA integrity_check").fetchone()[0]
        return result
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--out")
    args = parser.parse_args()
    result = audit(args.db)
    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps({"out": args.out, "ledger_and_oms_consistent": True, "real_submission_calls": 0}))
    else:
        print(json.dumps(result, indent=2))
