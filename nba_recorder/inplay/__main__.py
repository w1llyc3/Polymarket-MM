import argparse
import asyncio
import csv
import json
from dataclasses import replace
from pathlib import Path
from ..fair_value.store import read_connection
from ..quote_engine.storage import PaperStore
from .demo import AT, events
from .manager import UnifiedPaperManager
from .policy import BookPolicy
from .report import report
from .session import BookSession
from .store import SingleWriterStore


def main():
    parser = argparse.ArgumentParser(description="v2 BOOK_DRIVEN unified Paper research, no live mode")
    sub = parser.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("demo")
    demo.add_argument("--out", required=True)
    demo.add_argument("--split-shares", default="5", help="Explicit cash-funded paper collateral split")
    sports = sub.add_parser("sports-record")
    sports.add_argument("--db", required=True)
    sports.add_argument("--seconds", type=float, default=30)
    show = sub.add_parser("show")
    show.add_argument("--db", required=True)
    replay = sub.add_parser("replay")
    replay.add_argument("--db", required=True)
    replay.add_argument("--tape-db", required=True)
    replay.add_argument("--tape", required=True)
    replay.add_argument("--model", choices=("L0", "L1", "L2"), default="L2")
    replay.add_argument("--strategy", choices=("A", "B"), default="B")
    live = sub.add_parser("run-paper", help="One explicit game, public inputs and independent wall-clock watchdog; never live trading")
    live.add_argument("--db", required=True)
    live.add_argument("--source-db", required=True)
    live.add_argument("--event-slug", required=True)
    live.add_argument("--market-id", required=True)
    live.add_argument("--seconds", type=float, default=30)
    live.add_argument("--interval", type=float, default=1)
    live.add_argument("--model", choices=("L0", "L1", "L2"), default="L2")
    live.add_argument("--manual-file")
    live.add_argument("--contracts")
    live.add_argument("--operator-reset", action="store_true", help="Explicit operator request to reconcile and try reset; never automatic")
    confirm = sub.add_parser("confirm-phase", help="Write finite human Paper confirmation; not automatic sports evidence")
    for field in ("market-id", "event-id", "game-id", "operator-id", "evidence", "exit-instructions", "out"):
        confirm.add_argument("--"+field, required=True)
    confirm.add_argument("--period", type=int, required=True)
    confirm.add_argument("--remaining-s", type=int, required=True)
    confirm.add_argument("--duration-s", type=int, choices=range(1, 16), default=10)
    confirm.add_argument("--permission", choices=("ACTIVE_MM", "REDUCE_ONLY", "MM_CLOSED", "UNKNOWN", "PRE_GAME_OBSERVE"), required=True)
    confirm.add_argument("--clock-observation", choices=("RUNNING", "STOPPED", "UNKNOWN"), required=True)
    freeze = sub.add_parser("freeze-book")
    freeze.add_argument("--source", required=True)
    freeze.add_argument("--db", required=True)
    freeze.add_argument("--start-ms", type=int, required=True)
    freeze.add_argument("--end-ms", type=int, required=True)
    freeze.add_argument("--sports-source")
    freeze.add_argument("--contracts", help="Explicit reviewed game/clock/source-age/pause mapping JSON, absent => UNKNOWN phase")
    for command in ("reconcile", "manual-reset"):
        p = sub.add_parser(command)
        p.add_argument("--db", required=True)
    args = parser.parse_args()
    if args.command == "run-paper":
        from .runner import run_paper
        contracts = json.loads(Path(args.contracts).read_text(encoding="utf-8")) if args.contracts else None
        result = asyncio.run(run_paper(args.db, args.source_db, args.event_slug, args.market_id, args.seconds, args.interval, args.model, args.manual_file, contracts, args.operator_reset))
        print(json.dumps(result, indent=2))
        return
    if args.command == "confirm-phase":
        from ..api import utc_ms
        at = utc_ms()
        grant = {"authority_mode": "MANUAL_CONFIRMED", "confirmation_id": __import__('uuid').uuid4().hex, "market_id": args.market_id, "event_id": args.event_id,
                 "game_id": args.game_id, "operator_id": args.operator_id, "evidence": args.evidence, "exit_instructions": args.exit_instructions,
                 "period": args.period, "remaining_s": args.remaining_s, "permission": args.permission, "clock_observation": args.clock_observation,
                 "status_observation": "IN_PROGRESS", "confirmed_ms": at, "expires_ms": at+args.duration_s*1000, "not_unattended": True}
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        temp = out.with_suffix(out.suffix+".tmp")
        temp.write_text(json.dumps(grant, indent=2), encoding="utf-8")
        temp.replace(out)
        print(json.dumps({"file": str(out), "expires_ms": grant["expires_ms"], "not_unattended": True, "automatic_contract_passed": False}))
        return
    if args.command == "freeze-book":
        from .tape import freeze
        contracts = json.loads(Path(args.contracts).read_text(encoding="utf-8")) if args.contracts else {}
        store = PaperStore(args.db)
        try:
            tape_id = freeze(store, args.source, args.start_ms, args.end_ms, args.sports_source, contracts)
            print(json.dumps({"tape": tape_id, "strategy_mode": "BOOK_DRIVEN", "live_go": False}))
        finally:
            store.close()
        return
    if args.command == "sports-record":
        from .sports import record
        print(json.dumps(asyncio.run(record(args.db, args.seconds))))
        return
    if args.command == "show":
        db = read_connection(args.db)
        try:
            row = db.execute("SELECT time_ms,payload FROM ip_saved_report ORDER BY id DESC LIMIT 1").fetchone()
            control = db.execute("SELECT state,clock_ms,mode FROM pe_control WHERE id=1").fetchone()
            phases = [json.loads(r[0]) for r in db.execute("SELECT payload FROM ip_phase")]
            print(json.dumps({"current_control": dict(control) if control else None, "current_phases": phases,
                              "saved_report_ms": row["time_ms"] if row else None, "saved_report_can_be_stale": True,
                              "saved_report": json.loads(row["payload"]) if row else None, "real_submission_calls": 0}, indent=2))
        finally:
            db.close()
        return
    if args.command == "demo":
        out = Path(args.out)
        if out.exists():
            parser.error("choose a new evidence directory")
        out.mkdir(parents=True)
        stream = events()
        source = PaperStore(out/"tape.sqlite")
        tape = source.seal(stream, {"provenance": "synthetic", "start_ms": stream[0]["time_ms"], "end_ms": stream[-1]["time_ms"], "mode": "BOOK_DRIVEN"})
        source.close()
        results = []
        # A/B comparison holds funds, stream, model and delays constant. Delay
        # sensitivity is a separate factor, not mixed into the strategy effect.
        for model in ("L0", "L1", "L2"):
            for delay in (2, 20):
                for strategy in ("A", "B"):
                    store = SingleWriterStore(out/f"{strategy}-{model}-{delay}ms.sqlite")
                    try:
                        policy = BookPolicy(enhanced=strategy == "B", reduce_remaining_s=180 if strategy == "B" else 90, data_transport_ms=delay, cancel_latency_ms=10 if delay == 2 else 100)
                        om = UnifiedPaperManager(store, policy, AT)
                        om.split(stream[0]["frames"][0]["market"], args.split_shares, "explicit-demo-split")
                        session = BookSession(om, model)
                        for event in stream:
                            session.on_event(event)
                        result = report(om)
                        result.update(model=model, tape=tape, source="synthetic fixture, not actual game/odds", delay_sensitivity_group=delay)
                        save_report(om, result)
                        results.append(result)
                    finally:
                        store.close()
        (out/"comparison.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
        with (out/"comparison.csv").open("w", encoding="utf-8", newline="") as fp:
            writer = csv.writer(fp)
            writer.writerow(["strategy", "model", "delay_ms", "fill_events", "net_pnl", "risk_state", "ledger_consistent"])
            for r in results:
                writer.writerow([r["strategy"], r["model"], r["delay_sensitivity_group"], r["fill_events"], r["account"]["net_pnl"], r["risk_state"], r["ledger_and_oms_consistent"]])
        print(json.dumps({"tape": tape, "lanes": len(results), "all_ledger_consistent": all(r["ledger_and_oms_consistent"] for r in results), "comparison": str(out/"comparison.json"), "live_go": False}))
        return
    if args.command in {"reconcile", "manual-reset"} and not Path(args.db).exists():
        parser.error("existing unified paper database required")
    start_ms = 0
    if args.command == "replay":
        source = PaperStore.readonly(args.tape_db)
        try:
            start_ms = source.verify(args.tape)["start_ms"]
        finally:
            source.close()
    store = SingleWriterStore(args.db)
    try:
        saved = store.db.execute("SELECT config FROM pe_control WHERE id=1").fetchone()
        policy = BookPolicy(**json.loads(saved[0])) if saved else BookPolicy(enhanced=args.strategy == "B", reduce_remaining_s=180 if args.strategy == "B" else 90)
        if saved and args.command == "replay" and policy.enhanced != (args.strategy == "B"):
            raise ValueError("strategy is immutable; select the saved A/B strategy or a separate database")
        om = UnifiedPaperManager(store, policy, start_ms)
        if args.command in {"reconcile", "manual-reset"}:
            om.reconcile()
            if args.command == "manual-reset":
                om.resume()
        else:
            tape_store = PaperStore.readonly(args.tape_db)
            try:
                manifest = tape_store.verify(args.tape)
                if manifest.get("mode") != "BOOK_DRIVEN":
                    raise ValueError("explicit BOOK_DRIVEN tape required; no external fair fallback")
                bound = om.db.execute("SELECT payload FROM ip_session_config WHERE id='tape'").fetchone()
                if bound and bound[0] != args.tape:
                    raise ValueError("one immutable source tape per paper account")
                with om.db:
                    om.db.execute("INSERT OR IGNORE INTO ip_session_config VALUES('tape',?)", (args.tape,))
                session = BookSession(om, args.model)
                for row in tape_store.db.execute("SELECT payload FROM qe_input WHERE tape_id=? ORDER BY seq", (args.tape,)):
                    session.on_event(json.loads(row[0]))
            finally:
                tape_store.close()
        result = report(om)
        save_report(om, result)
        print(json.dumps(result, indent=2))
    finally:
        store.close()


def save_report(om, result):
    with om.db:
        om.db.execute("CREATE TABLE IF NOT EXISTS ip_saved_report(id INTEGER PRIMARY KEY,time_ms INTEGER,payload TEXT)")
        om.db.execute("INSERT INTO ip_saved_report(time_ms,payload) VALUES(?,?)", (om.now, json.dumps(result)))


if __name__ == "__main__":
    main()
