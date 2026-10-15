import argparse
import csv
import json
from pathlib import Path

from ..fair_value.providers import reject_secrets
from ..fair_value.store import read_connection
from ..storage import encode
from ..quote_engine.storage import PaperStore
from .hedge import evaluate_hedge
from .manager import OrderManager
from .risk import ExecutionPolicy
from .session import PaperSession
from .store import ExecutionStore


def policy_for(db, path=None):
    if path:
        values = json.loads(Path(path).read_text(encoding="utf-8"))
    else:
        row = db.execute("SELECT config FROM pe_control WHERE id=1").fetchone()
        values = json.loads(row[0]) if row else {}
    reject_secrets(values)
    return ExecutionPolicy(**values)


def main():
    p = argparse.ArgumentParser(description="PAPER only. No live mode, wallet, or secure SDK.")
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("demo", "replay", "show", "export", "kill", "reconcile", "resume"):
        s = sub.add_parser(name)
        s.add_argument("--db", default="data/lesson5-demo.sqlite")
        if name in ("demo", "replay"):
            s.add_argument("--tape-db", required=True)
            s.add_argument("--tape", required=True)
            s.add_argument("--policy")
            s.add_argument("--min-edge", default="0.01")
            s.add_argument("--max-frames", type=int)
        if name == "export":
            s.add_argument("--out", required=True)
    h = sub.add_parser("hedge")
    h.add_argument("--input", required=True, help="JSON arguments for research-only hedge evaluator")
    args = p.parse_args()
    if args.command == "hedge":
        data = json.loads(Path(args.input).read_text(encoding="utf-8"))
        reject_secrets(data)
        print(encode(evaluate_hedge(**data)))
        return
    if args.command in ("show", "export"):
        db = read_connection(args.db)
        try:
            if args.command == "show":
                control = db.execute("SELECT * FROM pe_control").fetchone()
                orders = [json.loads(r[0]) for r in db.execute("SELECT payload FROM pe_orders")]
                metrics = OrderManager.latency_summary(db)
                print(encode({"control": dict(control) if control else None, "orders": orders, "latency_summary": metrics, "latency_provenance": "synthetic_paper_clock", "real_submission_calls": 0}))
            else:
                out = Path(args.out)
                out.mkdir(parents=True, exist_ok=True)
                for table in ("pe_orders", "pe_journal", "pe_fills", "pe_exchange", "pe_risks", "pe_latency", "pe_quotes", "pe_stream"):
                    rows = db.execute(f"SELECT * FROM {table}")
                    with (out/(table+".csv")).open("w", encoding="utf-8", newline="") as fp:
                        writer = csv.writer(fp)
                        writer.writerow([d[0] for d in rows.description])
                        writer.writerows(rows)
                print(encode({"export": str(out.resolve()), "virtual": True}))
        finally:
            db.close()
        return
    if args.command == "demo" and Path(args.db).exists():
        raise ValueError("demo refuses to overwrite; choose a new --db")
    store = ExecutionStore(args.db)
    tape_store = None
    try:
        policy = policy_for(store.db, getattr(args, "policy", None))
        if args.command == "demo" and not args.policy:
            policy = ExecutionPolicy(initial_inventory="10", target_inventory="10")
        if args.command in ("demo", "replay"):
            # Existing sealed input is verified. This connection does not alter market events.
            if not Path(args.tape_db).exists():
                raise ValueError("existing Lesson 4 tape database required")
            tape_store = PaperStore.readonly(args.tape_db)
            manifest = tape_store.verify(args.tape)
            om = OrderManager(store, policy, manifest["start_ms"])
            PaperSession(om, args.min_edge).replay(tape_store, args.tape, 5 if args.command == "demo" else args.max_frames)
            if args.command == "demo":
                active = [o for o in om.orders() if o["state"] == "OPEN" and o["side"] == "BUY"]
                if not active:
                    raise ValueError("synthetic demo requires valid two-source fixture data, not live empty references")
                order = active[0]
                om.cancel(order["id"], drop_response=True)
                om.fill(order["id"], "2", "synthetic-cancel-race", transport_ms=2)
                om.advance(om.now+100)
                om.reconcile(latency_ms=101)
                om.reconcile()
                om.kill("synthetic-demo-final-halt")
        else:
            om = OrderManager(store, policy)
            if args.command == "kill":
                om.kill("operator_cli_kill")
            elif args.command == "reconcile":
                om.reconcile()
            elif args.command == "resume":
                om.reconcile()
                om.resume()
        print(encode(om.status()))
    finally:
        if tape_store:
            tape_store.close()
        store.close()


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as error:
        raise SystemExit(str(error))
