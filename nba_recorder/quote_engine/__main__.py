import argparse
import csv
import json
from dataclasses import asdict
from pathlib import Path

from ..fair_value.store import read_connection
from ..storage import encode
from .executor import Executor
from .model import Policy
from .storage import PaperStore
from .tape import freeze


def main():
    p = argparse.ArgumentParser(description="Offline virtual quotes only; zero real submissions")
    sub = p.add_subparsers(dest="command", required=True)
    f = sub.add_parser("freeze")
    f.add_argument("--source", required=True)
    f.add_argument("--db", required=True)
    f.add_argument("--start-ms", type=int, required=True)
    f.add_argument("--end-ms", type=int, required=True)
    f.add_argument("--step-ms", type=int, default=1000)
    r = sub.add_parser("run")
    r.add_argument("--db", required=True)
    r.add_argument("--tape", required=True)
    r.add_argument("--policy", help="JSON file with Policy fields; virtual cash/holdings only")
    r.add_argument("--max-frames", type=int)
    r.add_argument("--close", action="store_true")
    for command in ("show", "export"):
        s = sub.add_parser(command)
        s.add_argument("--db", required=True)
        s.add_argument("--run", required=True)
        if command == "export":
            s.add_argument("--out", required=True)
    d = sub.add_parser("demo")
    d.add_argument("--source", default="data/lesson4-synthetic-source.sqlite")
    d.add_argument("--db", default="data/lesson4-demo.sqlite")
    args = p.parse_args()
    if args.command in ("show", "export"):
        db = read_connection(args.db)
        try:
            run = db.execute("SELECT * FROM qe_runs WHERE id=?", (args.run,)).fetchone()
            if not run:
                raise ValueError("unknown run")
            if args.command == "show":
                counts = [dict(r) for r in db.execute("SELECT lane,state,count(*) AS count FROM qe_orders WHERE run_id=? GROUP BY lane,state", (args.run,))]
                latest = [json.loads(r[0]) for r in db.execute("SELECT payload FROM qe_decisions WHERE run_id=? AND input_seq=? ORDER BY lane", (args.run, run["cursor"]))]
                print(encode({"run": dict(run), "orders": counts, "latest_decisions": latest, "virtual": True}))
            else:
                out = Path(args.out)
                out.mkdir(parents=True, exist_ok=True)
                for table in ("qe_orders", "qe_journal", "qe_decisions", "qe_marks", "qe_accounts"):
                    rows = db.execute(f"SELECT * FROM {table} WHERE run_id=?", (args.run,))
                    with (out / (table+".csv")).open("w", newline="", encoding="utf-8") as fp:
                        writer = csv.writer(fp)
                        writer.writerow([d[0] for d in rows.description])
                        writer.writerows(rows)
                tape = db.execute("SELECT * FROM qe_tapes WHERE id=?", (run["tape_id"],)).fetchone()
                (out/"manifest.json").write_text(encode(dict(tape)), encoding="utf-8")
                with (out/"qe_input.csv").open("w", newline="", encoding="utf-8") as fp:
                    rows = db.execute("SELECT * FROM qe_input WHERE tape_id=? ORDER BY seq", (run["tape_id"],))
                    writer = csv.writer(fp)
                    writer.writerow([d[0] for d in rows.description])
                    writer.writerows(rows)
                print(encode({"export": str(out.resolve()), "real_submission_calls": 0}))
        finally:
            db.close()
        return
    if args.command == "demo":
        from .demo import demo
        print(encode(demo(args.source, args.db)))
        return
    store = PaperStore(args.db)
    try:
        if args.command == "freeze":
            print(encode({"tape": freeze(store, args.source, args.start_ms, args.end_ms, args.step_ms)}))
        else:
            policy = Policy(**json.loads(Path(args.policy).read_text(encoding="utf-8"))) if args.policy else Policy()
            executor = Executor(store, args.tape, policy)
            executor.run(args.max_frames, args.close)
            print(encode(executor.audit()))
    finally:
        store.close()


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as error:
        raise SystemExit(str(error))
