import argparse
import asyncio
import json
import os
import sqlite3
from pathlib import Path

from .api import PublicAPI, utc_ms
from .storage import Store
from .recorder import Recorder


def display(path, limit, depth, stale):
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    now = utc_ms()
    markets = db.execute("SELECT metadata FROM markets ORDER BY json_extract(metadata,'$.game_start_ms') LIMIT ?", (limit,)).fetchall()
    for (metadata,) in markets:
        m = json.loads(metadata)
        print(f"{m['question']} | market={m['market_id']} | {m['game_start']} | {m['status']}")
        for o in m["outcomes"]:
            row = db.execute("SELECT payload FROM latest_books WHERE asset_id=?", (o["asset_id"],)).fetchone()
            if not row:
                print(f"  {o['label']}: awaiting snapshot")
                continue
            s = json.loads(row[0])
            verified = db.execute("SELECT MAX(local_timestamp) FROM health WHERE kind='snapshot_verified' AND detail=?", (o["asset_id"],)).fetchone()[0]
            lifecycle = db.execute("SELECT kind FROM health WHERE kind IN ('connected','disconnected','stopped') ORDER BY id DESC LIMIT 1").fetchone()
            age = (now - s.get("book_receive_timestamp", s["local_receive_timestamp"])) / 1000
            flags = s["flags"].copy()
            if age > stale:
                flags.append("stale")
            if lifecycle and lifecycle[0] != "connected":
                flags.append("disconnected")
            if m["game_start_ms"] <= now:
                flags.append("not_pregame")
            if m["status"].get("tracking_state"):
                flags.append(m["status"]["tracking_state"])
            print(f"  {o['label']} asset={o['asset_id']} bid={s['bid']} ask={s['ask']} spread={s['spread']} age={age:.1f}s flags={sorted(set(flags))} tick={s['tick_size']} min={s['minimum_order_size']} verified_ms={verified}")
            print(f"    bids={s['bids'][:depth]} asks={s['asks'][:depth]}")
    if not markets:
        print("No recorded matching markets")
    db.close()


async def main(args):
    if args.command == "show":
        display(args.db, args.limit, args.depth, args.stale)
        return
    api = PublicAPI()
    try:
        result = await api.scan(scope=args.scope)
        if args.market_id:
            result["markets"] = [m for m in result["markets"] if m["market_id"] == args.market_id]
            if not result["markets"]:
                result["status"] = "no_matching_markets"
        print(json.dumps(result, indent=2, ensure_ascii=True), flush=True)
        if args.command == "record":
            store = Store(args.db)
            try:
                store.health(utc_ms(), "discovery", json.dumps(result))
                if result["markets"]:
                    recorder = Recorder(api, store, result["markets"][:args.limit], args.stale, args.resync, scope=args.scope)
                    recorder.limit = args.limit
                    await recorder.run(args.minutes * 60, args.force_reconnect_seconds)
                else:
                    store.health(utc_ms(), "no_matching_markets")
            finally:
                store.close()
    finally:
        await api.close()


def cli():
    p = argparse.ArgumentParser(description="Polymarket.com NBA 2026-27 public recorder; no accounts or orders")
    p.add_argument("command", choices=("scan", "record", "show"))
    p.add_argument("--scope", choices=("pregame", "full-game"), default="pregame", help="Collection only; never authorizes in-play quotes")
    p.add_argument("--market-id", help="Explicit market for full-game recording; prevents silently switching games")
    p.add_argument("--db", default=os.getenv("NBA_DB", "data/nba.sqlite"))
    p.add_argument("--limit", type=int, choices=(1, 2, 3), default=3)
    p.add_argument("--depth", type=int, default=5)
    p.add_argument("--stale", type=float, default=float(os.getenv("NBA_STALE_SECONDS", "30")))
    p.add_argument("--resync", type=float, default=float(os.getenv("NBA_RESYNC_SECONDS", "60")))
    p.add_argument("--minutes", type=float, default=30)
    p.add_argument("--force-reconnect-seconds", type=float, default=0, help="record-only transport fault test")
    args = p.parse_args()
    if args.command == "record" and args.scope == "full-game" and not args.market_id:
        p.error("full-game record requires explicit --market-id from scan")
    if args.minutes <= 0 or args.resync <= 0 or args.stale <= 0 or args.depth <= 0 or args.force_reconnect_seconds < 0:
        p.error("durations/depth must be positive")
    try:
        if args.command == "show":
            display(args.db, args.limit, args.depth, args.stale)
        else:
            asyncio.run(main(args))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print(json.dumps({"status": "error", "error_type": type(exc).__name__, "message": "Public-data operation failed; not a no-market result"}))
        raise SystemExit(1)


if __name__ == "__main__":
    cli()
