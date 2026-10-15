import argparse
import csv
import json
from pathlib import Path

from ..fair_value.providers import reject_secrets
from ..fair_value.store import read_connection
from ..quote_engine.model import Policy
from ..quote_engine.storage import PaperStore
from ..quote_engine.tape import freeze
from ..storage import encode
from .demo import create_source
from .engine import ResearchExecutor
from .models import ModelConfig
from .report import daily_report, persist_reports, report_days


def load(path, cls):
    values = json.loads(Path(path).read_text(encoding="utf-8")) if path else {}
    reject_secrets(values)
    return cls(**values)


def main():
    p = argparse.ArgumentParser(description="Lesson 6 paper fills/accounting; no live orders or exit execution")
    sub = p.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("demo")
    demo.add_argument("--source", default="data/lesson6-synthetic-source.sqlite")
    demo.add_argument("--db", default="data/lesson6-demo.sqlite")
    r = sub.add_parser("replay")
    r.add_argument("--tape-db", required=True)
    r.add_argument("--tape", required=True)
    r.add_argument("--db", required=True)
    r.add_argument("--policy")
    r.add_argument("--models")
    r.add_argument("--max-frames", type=int)
    r.add_argument("--close", action="store_true")
    r.add_argument("--resume-run", help="Restore the exact recorded policy/model configuration")
    imp = sub.add_parser("import-paper")
    imp.add_argument("--source", required=True)
    imp.add_argument("--db", required=True)
    cash = sub.add_parser("paper-event")
    cash.add_argument("--db", required=True)
    cash.add_argument("--run", required=True)
    cash.add_argument("--lane", required=True)
    cash.add_argument("--event", required=True, help="Explicit synthetic accounting JSON, closed runs only")
    for name in ("report", "export", "audit"):
        s = sub.add_parser(name)
        s.add_argument("--db", required=True)
        s.add_argument("--run", required=True)
        if name == "report":
            s.add_argument("--day", help="UTC YYYY-MM-DD; omit for every recorded day")
            s.add_argument("--out", help="Optional JSON output, read-only DB")
        if name == "export":
            s.add_argument("--out", required=True)
    args = p.parse_args()
    if args.command in ("report", "export", "audit"):
        db = read_connection(args.db)
        try:
            if args.command == "report":
                reports = [daily_report(db, args.run, d) for d in ([args.day] if args.day else report_days(db, args.run))]
                content = json.dumps(reports, indent=2)
                if args.out:
                    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
                    Path(args.out).write_text(content, encoding="utf-8")
                    print(encode({"report": str(Path(args.out).resolve()), "days": len(reports), "not_live_performance": True}))
                else:
                    print(content)
            elif args.command == "export":
                out = Path(args.out)
                out.mkdir(parents=True, exist_ok=True)
                for table in ("l6_events", "l6_order_status", "l6_closures", "l6_accounts", "l6_valuations", "l6_candidates", "l6_daily", "qe_orders", "qe_journal", "qe_marks"):
                    rows = db.execute(f"SELECT * FROM {table} WHERE run_id=?", (args.run,))
                    with (out/(table+".csv")).open("w", encoding="utf-8", newline="") as fp:
                        writer = csv.writer(fp)
                        writer.writerow([d[0] for d in rows.description])
                        writer.writerows(rows)
                print(encode({"export": str(out.resolve()), "real_submission_calls": 0}))
            else:
                from .ledger import Ledger
                checks = {lane: Ledger(db, args.run, lane).audit() for lane, in db.execute("SELECT lane FROM l6_accounts WHERE run_id=?", (args.run,))}
                if not checks:
                    raise ValueError("unknown accounting run")
                print(encode({"fifo_journal_verified": checks, "real_submission_calls": 0}))
        finally:
            db.close()
        return
    if args.command == "demo" and (Path(args.db).exists() or Path(args.source).exists()):
        raise ValueError("demo refuses to overwrite; choose new source/db paths")
    store = PaperStore(args.db)
    try:
        if args.command == "paper-event":
            row = store.db.execute("SELECT * FROM qe_runs WHERE id=?", (args.run,)).fetchone()
            saved = store.db.execute("SELECT payload FROM l6_config WHERE run_id=?", (args.run,)).fetchone()
            if not row or not saved:
                raise ValueError("unknown research run")
            engine = ResearchExecutor(store, row["tape_id"], Policy(**json.loads(row["config"])), ModelConfig(**json.loads(saved[0])))
            payload = json.loads(Path(args.event).read_text(encoding="utf-8"))
            reject_secrets(payload)
            changed = engine.record_paper_event(args.lane, payload)
            persist_reports(store.db, engine.run_id)
            print(encode({"recorded": changed, "run_id": engine.run_id, "lane": args.lane, "mode": "synthetic_paper_accounting", "real_submission_calls": 0}))
            return
        if args.command == "import-paper":
            from .paper_import import import_paper
            print(encode(import_paper(store, args.source)))
            return
        if args.command == "demo":
            from ..fair_value.demo import AT
            create_source(args.source)
            tape_id = freeze(store, args.source, AT, AT+350000)
            engine = ResearchExecutor(store, tape_id, Policy(initial_inventory="10", target_inventory="10"), ModelConfig(taker_fee_rate=".05", fee_source="synthetic_fee_assumption"))
            engine.run(close=True)
        else:
            source = PaperStore.readonly(args.tape_db)
            try:
                manifest = source.verify(args.tape)
                events = [json.loads(r[0]) for r in source.db.execute("SELECT payload FROM qe_input WHERE tape_id=? ORDER BY seq", (args.tape,))]
                tape_id = store.seal(events, {k: v for k, v in manifest.items() if k not in {"count", "chain_hash", "format_version"}})
                if tape_id != args.tape:
                    raise ValueError("copied tape identity mismatch")
            finally:
                source.close()
            if args.resume_run:
                row = store.db.execute("SELECT * FROM qe_runs WHERE id=?", (args.resume_run,)).fetchone()
                saved = store.db.execute("SELECT payload FROM l6_config WHERE run_id=?", (args.resume_run,)).fetchone()
                if not row or not saved or row["tape_id"] != tape_id:
                    raise ValueError("resume run/tape identity mismatch")
                if args.policy or args.models:
                    raise ValueError("resume uses saved configuration; omit --policy/--models")
                engine = ResearchExecutor(store, tape_id, Policy(**json.loads(row["config"])), ModelConfig(**json.loads(saved[0])))
                if engine.run_id != args.resume_run:
                    raise ValueError("execution version mismatch; cannot resume under a different model")
            else:
                engine = ResearchExecutor(store, tape_id, load(args.policy, Policy), load(args.models, ModelConfig))
            engine.run(args.max_frames, args.close)
        reports = persist_reports(store.db, engine.run_id)
        print(encode({**engine.audit(), "tape": tape_id, "days": [r["day"] for r in reports], "models": ["L0", "L1", "L2"], "lane_count": 9, "mode": "PAPER", "not_live_performance": True}))
    finally:
        store.close()


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as error:
        raise SystemExit(str(error))
