import asyncio
import json
import random
import time
from contextlib import asynccontextmanager

from websockets.asyncio.client import connect

from .api import WS, utc_ms
from .book import Book, ResyncRequired


class Recorder:
    def __init__(self, api, store, markets, stale=30, resync=60, scope="pregame"):
        if scope not in {"pregame", "full-game"}:
            raise ValueError("unsupported collection scope")
        self.scope = scope
        self.tracked_market_ids = {m["market_id"] for m in markets}
        self.api, self.store = api, store
        self.books = {o["asset_id"]: Book(m, o["asset_id"]) for m in markets for o in m["outcomes"]}
        self.stale, self.resync = stale, resync
        self.limit = 3
        self.resolved = {str(a) for (payload,) in store.db.execute("SELECT payload FROM raw_events WHERE event_type='market_resolved'") for a in json.loads(payload).get("assets_ids", [])}
        for m in markets:
            if any(o["asset_id"] in self.resolved for o in m["outcomes"]):
                m["status"]["tracking_state"] = "resolved"
        self.store.markets(markets, utc_ms())

    def health(self, kind, detail=""):
        self.store.health(utc_ms(), kind, detail)
        print(json.dumps({"health": kind, "detail": detail, "local_timestamp": utc_ms()}), flush=True)

    async def refresh(self, assets):
        for asset in assets:
            book = self.books[asset]
            book.invalidate()
            event = await self.api.book(asset)
            received = utc_ms()
            if not isinstance(event, dict):
                self.store.commit({"event_type": "invalid_rest_snapshot", "response": event}, "rest", received, [])
                raise ResyncRequired("REST schema mismatch")
            event["event_type"] = "rest_snapshot"
            try:
                if str(event.get("asset_id")) != asset or event.get("market") != book.market["condition_id"]:
                    raise ResyncRequired("REST identity mismatch")
                book.apply(event, received)
            except (ValueError, KeyError, TypeError, ArithmeticError):
                self.store.commit(event, "rest", received, [])
                raise
            self.store.commit(event, "rest", received, [book.snapshot(received, self.stale)])
            # A duplicate snapshot still refreshes in-memory state and verification time.
            self.store.health(received, "snapshot_verified", asset)

    async def rediscover(self):
        result = await self.api.scan(scope=self.scope) if self.scope == "full-game" else await self.api.scan()
        markets = ([m for m in result["markets"] if m["market_id"] in self.tracked_market_ids] if self.scope == "full-game" else result["markets"][:self.limit])
        for m in markets:
            if any(o["asset_id"] in self.resolved for o in m["outcomes"]):
                m["status"]["tracking_state"] = "resolved"
        self.store.markets(markets, utc_ms())
        self.store.health(utc_ms(), "rediscovery", json.dumps(result))
        desired = {o["asset_id"]: m for m in markets for o in m["outcomes"]}
        old_markets = {b.market["market_id"]: b.market for b in self.books.values()}
        desired_ids = {m["market_id"] for m in markets}
        for market_id, m in old_markets.items():
            if market_id not in desired_ids:
                m["status"]["tracking_state"] = "no_longer_eligible"
                self.store.markets([m], utc_ms())
        desired = {a: m for a, m in desired.items() if a not in self.resolved}
        changed = set(desired) != set(self.books)
        self.books = {a: self.books[a] if a in self.books else Book(m, a) for a, m in desired.items()}
        for a, m in desired.items():
            self.books[a].market = m
        return changed

    async def process(self, event, received):
        kind = event.get("event_type", "unknown")
        event = {**event, "event_type": kind}
        key = self.store.key(event, "ws")
        if self.store.contains(key):
            self.store.health(received, "duplicate", kind)
            if kind != "market_resolved":
                return
        if kind == "price_change" and (not isinstance(event.get("price_changes"), list) or not event["price_changes"] or any(not isinstance(c, dict) or not c.get("asset_id") for c in event["price_changes"])):
            self.store.commit(event, "ws", received, [], key)
            for book in self.books.values():
                book.invalidate()
            raise ResyncRequired("invalid_delta_schema")
        if kind == "book" and not event.get("asset_id"):
            self.store.commit(event, "ws", received, [], key)
            for book in self.books.values():
                book.invalidate()
            raise ResyncRequired("book_missing_asset")
        assets = {str(event.get("asset_id"))} if event.get("asset_id") else {str(c.get("asset_id")) for c in event.get("price_changes", [])}
        if kind == "market_resolved":
            assets.update(str(a) for a in event.get("assets_ids", []))
        assets &= self.books.keys()
        snapshots, repair = [], set()
        for asset in sorted(assets):
            book = self.books[asset]
            try:
                if event.get("market") != book.market["condition_id"]:
                    raise ResyncRequired("WS identity mismatch")
                if kind == "market_resolved":
                    book.invalidate()
                    self.resolved.add(asset)
                    book.market["status"]["tracking_state"] = "resolved"
                    self.store.markets([book.market], received)
                    self.health("market_resolved", asset)
                    raise ResyncRequired("resolved market; stop subscription")
                if kind not in {"book", "price_change", "tick_size_change", "best_bid_ask", "last_trade_price", "new_market"}:
                    raise ResyncRequired("unknown_asset_event")
                applied = book.apply(event, received)
                if kind in ("price_change", "tick_size_change", "best_bid_ask"):
                    book.invalidate()
                    repair.add(asset)
                if applied:
                    snapshots.append(book.snapshot(received, self.stale))
            except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
                book.invalidate()
                rejected = book.snapshot(received, self.stale)
                rejected["book_receive_timestamp"] = rejected["local_receive_timestamp"]
                rejected["local_receive_timestamp"] = received
                snapshots.append(rejected)
                self.store.health(received, "resync_required", type(exc).__name__ + ": " + str(exc))
                repair.add(asset)
        self.store.commit(event, "ws", received, snapshots, key)
        if kind == "market_resolved":
            raise ResyncRequired("market resolved")
        if repair:
            await self.refresh(sorted(repair))

    @asynccontextmanager
    async def heartbeat(self, ws):
        async def ping():
            while True:
                await asyncio.sleep(10)
                await ws.send("PING")
        task = asyncio.create_task(ping())
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def run(self, seconds, force_reconnect=0):
        deadline = time.monotonic() + seconds
        attempts = 0
        self.health("started", "record-only")
        scan_interval = 20 if self.scope == "full-game" else 300
        next_scan = time.monotonic() + scan_interval
        try:
            while time.monotonic() < deadline:
                for b in self.books.values():
                    b.invalidate()
                # Drop assets at advertised start time. Never continue into live games.
                assets = [a for a, b in self.books.items() if (self.scope == "full-game" or b.market["game_start_ms"] > utc_ms()) and a not in self.resolved]
                if not assets:
                    self.health("no_pregame_assets")
                    await asyncio.sleep(min(60, max(0, deadline - time.monotonic())))
                    if time.monotonic() < deadline:
                        await self.rediscover()
                    continue
                try:
                    async with connect(WS, ping_interval=None, open_timeout=15, close_timeout=5, max_queue=1024) as ws, self.heartbeat(ws):
                        await ws.send(json.dumps({"assets_ids": assets, "type": "market", "custom_feature_enabled": True}))
                        self.health("connected", str(len(assets)))
                        await self.refresh(assets)
                        connected = time.monotonic()
                        next_refresh = connected + min(self.resync, self.stale)
                        last_pong = connected
                        while time.monotonic() < deadline:
                            now = time.monotonic()
                            if now >= next_scan:
                                changed = await self.rediscover()
                                next_scan = time.monotonic() + scan_interval
                                if changed:
                                    raise ResyncRequired("subscription_set_changed")
                            if force_reconnect and now - connected >= force_reconnect:
                                raise ResyncRequired("forced_reconnect_test")
                            if self.scope == "pregame" and any(self.books[a].market["game_start_ms"] <= utc_ms() for a in assets):
                                raise ResyncRequired("game_start_subscription_refresh")
                            if now - last_pong > 30:
                                raise ResyncRequired("heartbeat_timeout")
                            if now >= next_refresh:
                                await self.refresh(assets)
                                next_refresh = time.monotonic() + min(self.resync, self.stale)
                            try:
                                raw = await asyncio.wait_for(ws.recv(), timeout=min(1, max(0.01, deadline - now)))
                            except asyncio.TimeoutError:
                                continue
                            received = utc_ms()
                            if raw == "PONG":
                                last_pong = time.monotonic()
                                attempts = 0
                                continue
                            try:
                                messages = json.loads(raw)
                            except (ValueError, TypeError):
                                self.store.commit({"event_type": "invalid_frame", "frame": str(raw)}, "ws", received, [])
                                raise ResyncRequired("invalid_json_frame")
                            for event in messages if isinstance(messages, list) else [messages]:
                                if not isinstance(event, dict):
                                    raise ResyncRequired("invalid_event_schema")
                                await self.process(event, received)
                except (OSError, TimeoutError, ValueError, KeyError, TypeError, ArithmeticError) as exc:
                    self.health("disconnected", type(exc).__name__ + (": " + str(exc) if isinstance(exc, ResyncRequired) else ""))
                except Exception as exc:
                    # Transport errors never log headers, environment or account data.
                    self.health("disconnected", type(exc).__name__)
                for b in self.books.values():
                    b.invalidate()
                if time.monotonic() < deadline:
                    await asyncio.sleep(min(30, 2 ** min(attempts, 5) + random.random(), max(0, deadline-time.monotonic())))
                    attempts += 1
        finally:
            for b in self.books.values():
                b.invalidate()
            self.health("stopped")
