import argparse
import asyncio
import json
import math
import time
from pathlib import Path

from ..api import utc_ms
from .demo import create_demo
from .engine import Config, evaluate, market_ids_at, table_exists
from .markouts import update_markouts
from .providers import JSONProvider, ProviderError, TheOddsAPIProvider
from .store import FairStore, read_connection, digest


def dump(value):
    print(json.dumps(value, ensure_ascii=True, indent=2), flush=True)


def config(args):
    return Config(odds_max_age_ms=args.odds_age * 1000, book_max_age_ms=args.book_age * 1000,
                  metadata_max_age_ms=args.metadata_age * 1000, start_tolerance_ms=args.start_tolerance_ms,
                  max_source_range=args.conflict_range, min_edge=args.min_edge, markout_window_ms=args.markout_window_ms)


def evaluate_all(db, args, at):
    ids = [args.market_id] if args.market_id else market_ids_at(db, at)
    result = [s for m in ids for s in evaluate(db, m, at, config(args))]
    return result or [{"status": "NO_QUOTE", "reasons": ["no_asof_markets"], "decision_ms": at, "auto_live_quote_allowed": False}]


async def collect_once(store, args):
    providers = [JSONProvider(path, "local-json:" + digest(str(Path(path).resolve()))[:12] + ":" + Path(path).name) for path in args.json]
    if args.provider == "the-odds-api":
        contracts = json.loads(Path(args.rules).read_text(encoding="utf-8-sig")) if args.rules else {}
        providers.append(TheOddsAPIProvider(args.regions, args.odds_format, contracts))
    if not providers:
        raise ProviderError("Choose --provider the-odds-api or one or more --json paths")
    results = []
    for provider in providers:
        try:
            batch = await provider.fetch()
            raw_id = store.ingest(batch)
            results.append({"status": "collected", "provider": batch.provider, "raw_id": raw_id, "quotes": len(batch.quotes), "received_ms": batch.received_ms, "data_kind": batch.data_kind})
        except ProviderError as exc:
            # Only our sanitized provider exceptions are displayed.
            results.append({"status": "provider_error", "message": str(exc)})
    return results


async def collect(args, run=False):
    store = FairStore(args.db)
    try:
        deadline = time.monotonic() + args.minutes * 60
        while True:
            collections = await collect_once(store, args)
            now = utc_ms()
            signals = evaluate_all(store.db, args, now)
            if run:
                for signal in signals:
                    if "id" in signal:
                        store.persist(signal)
                markouts = update_markouts(store, now)
            else:
                markouts = []
            dump({"stage": "record_only", "collections": collections, "signals": signals, "markouts_updated": len(markouts)})
            if not run or time.monotonic() >= deadline:
                return any(c["status"] == "collected" for c in collections)
            await asyncio.sleep(min(args.interval, max(0, deadline - time.monotonic())))
    finally:
        store.close()


def cli():
    p = argparse.ArgumentParser(description="Lesson 3: odds references and hypothetical markouts; never executes orders")
    p.add_argument("command", choices=("demo", "ingest", "evaluate", "record", "run", "markouts", "show"))
    p.add_argument("--db", default="data/nba.sqlite")
    p.add_argument("--demo-db", default="data/lesson3-demo.sqlite")
    p.add_argument("--json", action="append", default=[], help="normalized authorized JSON snapshot; may repeat")
    p.add_argument("--provider", choices=("the-odds-api",))
    p.add_argument("--rules", help="verified per-bookmaker rule contracts JSON; absent rules block matching")
    p.add_argument("--regions", default="us")
    p.add_argument("--odds-format", choices=("decimal", "american"), default="decimal")
    p.add_argument("--market-id")
    p.add_argument("--at-ms", type=int, help="UTC decision timestamp; no future rows are used")
    p.add_argument("--odds-age", type=int, default=120)
    p.add_argument("--book-age", type=int, default=30)
    p.add_argument("--metadata-age", type=int, default=360)
    p.add_argument("--start-tolerance-ms", type=int, default=0)
    p.add_argument("--conflict-range", default="0.05")
    p.add_argument("--min-edge", default="0.01")
    p.add_argument("--markout-window-ms", type=int, default=5000)
    p.add_argument("--minutes", type=float, default=1)
    p.add_argument("--interval", type=float, default=30)
    p.add_argument("--limit", type=int, default=10)
    args = p.parse_args()
    try:
        config(args)
        if not math.isfinite(args.minutes) or not math.isfinite(args.interval) or args.minutes <= 0 or args.interval <= 0 or args.limit <= 0:
            p.error("durations and limit must be positive and finite")
        if args.command in {"run", "ingest"} and args.at_ms is not None:
            p.error("collection cannot backdate receipt/decision timestamps")
        at = args.at_ms if args.at_ms is not None else utc_ms()
        if at < 0 or at > utc_ms():
            p.error("--at-ms must be a nonnegative past/current timestamp")
        if args.command == "demo":
            dump(create_demo(args.demo_db))
        elif args.command in {"run", "ingest"}:
            if not asyncio.run(collect(args, args.command == "run")):
                raise SystemExit(1)
        elif args.command in {"record", "markouts"}:
            store = FairStore(args.db)
            try:
                if args.command == "record":
                    signals = evaluate_all(store.db, args, at)
                    for s in signals:
                        if "id" in s:
                            store.persist(s)
                    dump(signals)
                else:
                    dump(update_markouts(store, at))
            finally:
                store.close()
        else:
            db = read_connection(args.db)
            try:
                if args.command == "evaluate":
                    dump(evaluate_all(db, args, at))
                elif not table_exists(db, "fv_signals"):
                    dump({"status": "NO_QUOTE", "reasons": ["no_recorded_signals"]})
                else:
                    rows = db.execute("SELECT payload FROM fv_signals WHERE decision_ms<=? ORDER BY decision_ms DESC,id LIMIT ?", (at, args.limit))
                    dump([json.loads(r[0]) for r in rows])
            finally:
                db.close()
    except KeyboardInterrupt:
        pass
    except ProviderError as exc:
        dump({"status": "NO_QUOTE", "reasons": [str(exc)]})
        raise SystemExit(1)
    except Exception as exc:
        dump({"status": "NO_QUOTE", "reasons": ["operation_failed"], "error_type": type(exc).__name__})
        raise SystemExit(1)


if __name__ == "__main__":
    cli()
