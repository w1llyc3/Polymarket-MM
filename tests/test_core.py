import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nba_recorder.api import CLOB, GAMMA, PublicAPI, select_market
from nba_recorder.book import Book, ResyncRequired
from nba_recorder.storage import Store
from nba_recorder.recorder import Recorder


def fixture():
    e = {"id": "1", "slug": "nba-bos-det-2026-10-20", "series": [{"id": "10345"}], "active": True, "startTime": "2026-10-20T19:00:00Z"}
    e["teams"] = [{"name": "Celtics", "abbreviation": "bos", "league": "nba", "ordering": "away"}, {"name": "Pistons", "abbreviation": "det", "league": "nba", "ordering": "home"}]
    m = {"id": "2", "slug": e["slug"], "question": "Celtics vs. Pistons", "conditionId": "condition", "sportsMarketType": "moneyline", "active": True, "enableOrderBook": True, "acceptingOrders": True, "clobTokenIds": '["a","b"]', "outcomes": '["Celtics","Pistons"]', "orderPriceMinTickSize": .01, "orderMinSize": 5}
    selected, _ = select_market(e, m, "10345", 0)
    return e, m, selected


def full(timestamp="100"):
    return {"event_type": "book", "asset_id": "a", "market": "condition", "timestamp": timestamp, "bids": [{"price": "0.4", "size": "10"}], "asks": [{"price": "0.6", "size": "20"}]}


class ScannerTests(unittest.TestCase):
    def test_per_event_roles_override_league_display_order(self):
        e, m, _ = fixture()
        result, _ = select_market(e, m, "10345", 0, "home")
        self.assertEqual(result["home_team"], "det")
        self.assertEqual(result["away_team"], "bos")
        self.assertEqual(result["home_away_evidence"], "event_teams")

    def test_missing_roles_never_guessed_from_slug(self):
        e, m, _ = fixture()
        e.pop("teams")
        result, _ = select_market(e, m, "10345", 0, "away")
        self.assertIsNone(result["home_team"])
        self.assertIsNone(result["home_away_evidence"])

    def test_named_outcomes_not_false_yes_no(self):
        _, _, m = fixture()
        self.assertIsNone(m["yes_asset_id"])
        self.assertEqual(m["outcomes"][0], {"label": "Celtics", "asset_id": "a"})

    def test_yes_no_and_v2(self):
        e, m, _ = fixture()
        m.update(outcomes='["Yes","No"]', version="v2", positionIds=["p", "q"])
        result, _ = select_market(e, m, "10345", 0)
        self.assertEqual(result["yes_asset_id"], "p")
        self.assertEqual(result["no_asset_id"], "q")

    def test_exclusion_matrix(self):
        for field, value in [("sportsMarketType", "spreads"), ("closed", True), ("active", False), ("gameStartTime", "2020-01-01T00:00:00Z"), ("clobTokenIds", "[]"), ("slug", "nba-bos-det-2026-10-20-halftime")]:
            with self.subTest(field=field):
                e, m, _ = fixture()
                m[field] = value
                result, _ = select_market(e, m, "10345", 1)
                self.assertIsNone(result)
        for date in ["2026-10-14", "2026-12-11", "2027-02-20", "2027-04-12"]:
            e, m, _ = fixture()
            e["slug"] = m["slug"] = "nba-bos-det-" + date
            self.assertIsNone(select_market(e, m, "10345", 0)[0])

    def test_started_and_live(self):
        e, m, selected = fixture()
        self.assertIsNone(select_market(e, m, "10345", selected["game_start_ms"])[0])
        e["live"] = True
        self.assertIsNone(select_market(e, m, "10345", 0)[0])

    def test_unrelated_series(self):
        e, m, _ = fixture()
        self.assertIsNone(select_market(e, m, "US", 0)[0])


class BookTests(unittest.TestCase):
    def setUp(self):
        self.b = Book(fixture()[2], "a")

    def test_depth_sorted_decimal(self):
        e = full()
        e["bids"].append({"price": "0.41", "size": "1.23456789"})
        self.b.apply(e, 1000)
        s = self.b.snapshot(1000)
        self.assertEqual(s["bid"], "0.41")
        self.assertEqual(s["spread"], "0.19")
        self.assertEqual(s["bids"][0]["size"], "1.23456789")

    def test_absolute_delta_and_delete(self):
        self.b.apply(full(), 1000)
        self.b.apply({"event_type": "price_change", "timestamp": "101", "price_changes": [{"asset_id": "a", "side": "BUY", "price": "0.4", "size": "2"}]}, 1001)
        self.assertEqual(self.b.snapshot(1001)["bids"][0]["size"], "2")
        self.assertFalse(self.b.synced)
        self.b.apply(full("102"), 1002)
        self.b.apply({"event_type": "price_change", "timestamp": "103", "price_changes": [{"asset_id": "a", "side": "BUY", "price": "0.4", "size": "0"}]}, 1003)
        self.assertIn("empty", self.b.snapshot(1003)["flags"])

    def test_delta_before_snapshot(self):
        with self.assertRaises(ResyncRequired):
            self.b.apply({"event_type": "price_change", "price_changes": []}, 1000)

    def test_older_snapshot_and_delta_rejected(self):
        self.b.apply(full(), 1000)
        for e in [full("99"), {"event_type": "price_change", "timestamp": "99", "price_changes": []}]:
            with self.assertRaises(ResyncRequired):
                self.b.apply(e, 1001)

    def test_crossed_locked_empty_never_clamped(self):
        for ask, flag in [("0.3", "crossed"), ("0.4", "locked")]:
            e = full()
            e["asks"][0]["price"] = ask
            self.b.apply(e, 1000)
            s = self.b.snapshot(1000)
            self.assertIn(flag, s["flags"])
            self.assertEqual(s["ask"], ask)
        e["asks"] = []
        self.b.apply(e, 1000)
        self.assertIn("empty", self.b.snapshot(1000)["flags"])

    def test_stale_and_disconnect(self):
        self.b.apply(full(), 1000)
        self.b.invalidate()
        self.assertEqual(set(self.b.snapshot(32000)["flags"]), {"stale", "unverified_continuity"})

    def test_invalid_numbers_atomic(self):
        self.b.apply(full(), 1000)
        for value in ["NaN", "Infinity", "-0.1", "1.1"]:
            e = full("101")
            e["asks"][0]["price"] = value
            with self.assertRaises(ResyncRequired):
                self.b.apply(e, 1001)
            self.assertEqual(self.b.snapshot(1001)["ask"], "0.6")

    def test_invalid_constraints_and_tick_updates(self):
        self.b.apply(full(), 1000)
        for field in ["tick_size", "min_order_size"]:
            e = full("101")
            e[field] = "0"
            with self.assertRaises(ResyncRequired):
                self.b.apply(e, 1001)
        self.b.apply({"event_type": "tick_size_change", "new_tick_size": "0.001", "timestamp": "102"}, 1002)
        self.assertEqual(self.b.snapshot(1002)["tick_size"], "0.001")


class StorageTests(unittest.TestCase):
    def test_restart_dedupe_atomic_snapshot(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "test.sqlite")
            s = Store(path)
            b = Book(fixture()[2], "a")
            b.apply(full(), 1000)
            self.assertTrue(s.commit(full(), "ws", 1000, [b.snapshot(1000)]))
            s.close()
            s = Store(path)
            self.assertFalse(s.commit(full(), "ws", 1001, [b.snapshot(1001)]))
            self.assertEqual(s.db.execute("SELECT count(*) FROM snapshots").fetchone()[0], 1)
            self.assertEqual(s.db.execute("SELECT local_receive_timestamp FROM raw_events").fetchone()[0], 1000)
            latest = json.loads(s.db.execute("SELECT payload FROM latest_books").fetchone()[0])
            self.assertEqual(latest["flags"], [])
            s.close()

    def test_duplicate_rest_refreshes_latest_without_duplicate_history(self):
        with tempfile.TemporaryDirectory() as d:
            s = Store(str(Path(d) / "test.sqlite"))
            b = Book(fixture()[2], "a")
            b.apply(full(), 1000)
            s.commit(full(), "rest", 1000, [b.snapshot(1000)])
            b.apply(full(), 2000)
            self.assertFalse(s.commit(full(), "rest", 2000, [b.snapshot(2000)]))
            latest = json.loads(s.db.execute("SELECT payload FROM latest_books").fetchone()[0])
            self.assertEqual(latest["local_receive_timestamp"], 2000)
            self.assertEqual(s.db.execute("SELECT count(*) FROM snapshots").fetchone()[0], 1)
            s.close()

    def test_malformed_timestamp_raw_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            s = Store(str(Path(d) / "test.sqlite"))
            self.assertTrue(s.commit({"event_type": "book", "timestamp": "broken"}, "ws", 1000, []))
            self.assertIsNone(s.db.execute("SELECT exchange_timestamp FROM raw_events").fetchone()[0])
            s.close()

    def test_missing_identity_not_over_deduped(self):
        with tempfile.TemporaryDirectory() as d:
            s = Store(str(Path(d) / "test.sqlite"))
            e = {"event_type": "unknown"}
            self.assertNotEqual(s.key(e, "ws"), s.key(e, "ws"))
            for e in [{"event_type": "new_market", "id": "unique"}, {"event_type": "last_trade_price", "transaction_hash": "tx"}]:
                self.assertEqual(s.key(e, "ws"), s.key(e, "ws"))
            s.close()


class IntegrationUnitTests(unittest.IsolatedAsyncioTestCase):
    @patch('nba_recorder.api.utc_ms',return_value=1792400000000)
    async def test_missing_list_roles_fetches_public_event_detail(self,_clock):
        api = PublicAPI()
        e, m, _ = fixture()
        detailed = {**e, "markets": [m]}
        brief = copy.deepcopy(detailed)
        brief.pop("teams")
        calls = []
        async def get(host, path, params=None):
            calls.append(path)
            if path == "/sports": return [{"sport":"nba", "series":"10345", "ordering":"away"}]
            if path == "/events": return [brief]
            return detailed
        api.get = get
        try:
            result = await api.scan()
            self.assertEqual(result["markets"][0]["home_away_evidence"], "event_teams")
            self.assertIn("/events/slug/nba-bos-det-2026-10-20", calls)
        finally:
            await api.close()

    async def test_resolved_assets_are_not_resubscribed(self):
        with tempfile.TemporaryDirectory() as d:
            s = Store(str(Path(d) / "test.sqlite"))
            r = Recorder(None, s, [fixture()[2]])
            event = {"event_type": "market_resolved", "market": "condition", "assets_ids": ["a", "b"], "timestamp": "101"}
            with self.assertRaises(ResyncRequired):
                await r.process(event, 1001)
            self.assertEqual(r.resolved, {"a", "b"})
            self.assertTrue(all(not b.synced for b in r.books.values()))
            status = json.loads(s.db.execute("SELECT metadata FROM markets").fetchone()[0])["status"]
            self.assertEqual(status["tracking_state"], "resolved")
            # Restarted in-memory state must honor a previously recorded resolution.
            r.resolved.clear()
            with self.assertRaises(ResyncRequired):
                await r.process(event, 1002)
            self.assertEqual(r.resolved, {"a", "b"})
            self.assertEqual(s.db.execute("SELECT count(*) FROM raw_events").fetchone()[0], 1)
            class FakeAPI:
                async def scan(self):
                    return {"markets": [fixture()[2]], "status": "ok"}
            restarted = Recorder(FakeAPI(), s, [fixture()[2]])
            self.assertEqual(restarted.resolved, {"a", "b"})
            await restarted.rediscover()
            self.assertFalse(restarted.books)
            status = json.loads(s.db.execute("SELECT metadata FROM markets").fetchone()[0])["status"]
            self.assertEqual(status["tracking_state"], "resolved")
            s.close()

    async def test_missing_asset_delta_preserved_and_invalidates_all(self):
        with tempfile.TemporaryDirectory() as d:
            s = Store(str(Path(d) / "test.sqlite"))
            r = Recorder(None, s, [fixture()[2]])
            for b in r.books.values():
                b.synced = True
            with self.assertRaises(ResyncRequired):
                await r.process({"event_type": "price_change", "timestamp": "101", "price_changes": [{"price": "0.4", "size": "5", "side": "BUY"}]}, 1001)
            self.assertTrue(all(not b.synced for b in r.books.values()))
            self.assertEqual(s.db.execute("SELECT count(*) FROM raw_events").fetchone()[0], 1)
            s.close()

    async def test_bad_rest_preserved_before_reconnect(self):
        class FakeAPI:
            async def book(self, asset):
                return {"asset_id": "wrong", "market": "wrong", "timestamp": "100"}
        with tempfile.TemporaryDirectory() as d:
            s = Store(str(Path(d) / "test.sqlite"))
            r = Recorder(FakeAPI(), s, [fixture()[2]])
            with self.assertRaises(ResyncRequired):
                await r.refresh(["a"])
            self.assertFalse(r.books["a"].synced)
            self.assertEqual(s.db.execute("SELECT count(*) FROM raw_events").fetchone()[0], 1)
            s.close()

    @patch('nba_recorder.recorder.utc_ms',return_value=1792400000000)
    async def test_transport_reconnect_resnapshots_and_stops(self,_clock):
        import nba_recorder.recorder as module
        real_sleep = asyncio.sleep
        class FakeAPI:
            calls = 0
            async def book(self, asset):
                self.calls += 1
                e = full()
                e["asset_id"] = asset
                return e
        class FakeWS:
            sent = []
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                return False
            async def send(self, value):
                self.sent.append(value)
            async def recv(self):
                await real_sleep(.005)
                return "PONG"
        async def fast_sleep(seconds):
            await real_sleep(min(seconds, .005))
        with tempfile.TemporaryDirectory() as d:
            s = Store(str(Path(d) / "test.sqlite"))
            api, ws = FakeAPI(), FakeWS()
            r = Recorder(api, s, [fixture()[2]])
            with patch.object(module, "connect", return_value=ws), patch.object(module.asyncio, "sleep", side_effect=fast_sleep):
                await r.run(.18, force_reconnect=.04)
            connections = s.db.execute("SELECT count(*) FROM health WHERE kind='connected'").fetchone()[0]
            self.assertGreaterEqual(connections, 2)
            self.assertGreaterEqual(api.calls, connections * 2)
            self.assertTrue(all(not b.synced for b in r.books.values()))
            self.assertEqual(s.db.execute("SELECT kind FROM health ORDER BY id DESC LIMIT 1").fetchone()[0], "stopped")
            for sent in ws.sent:
                if sent != "PING":
                    self.assertEqual(json.loads(sent)["type"], "market")
                    self.assertNotIn("auth", json.loads(sent))
            s.close()

    async def test_no_market_is_distinct_from_failure(self):
        api = PublicAPI()
        async def get(host, path, params=None):
            return [{"sport": "nba", "series": "10345"}] if path == "/sports" else []
        api.get = get
        try:
            self.assertEqual((await api.scan())["status"], "no_matching_markets")
            async def broken(*args, **kwargs):
                raise ValueError("schema failure")
            api.get = broken
            with self.assertRaises(ValueError):
                await api.scan()
        finally:
            await api.close()

    async def test_allowlist_rejects_trading_and_us(self):
        api = PublicAPI()
        try:
            for host, path in [(CLOB, "/order"), (CLOB, "/balance-allowance"), ("https://api.polymarket.us", "/markets")]:
                with self.assertRaises(ValueError):
                    await api.get(host, path)
        finally:
            await api.close()

    @patch('nba_recorder.api.utc_ms',return_value=1792400000000)
    async def test_pagination_no_market_and_errors(self,_clock):
        api = PublicAPI()
        calls = []
        e, m, _ = fixture()
        e["markets"] = [m]
        async def get(host, path, params=None):
            calls.append((path, params))
            if path == "/sports":
                return [{"sport": "nba", "series": "10345"}]
            return [copy.deepcopy(e) for _ in range(100)] if params["offset"] == 0 else []
        api.get = get
        try:
            result = await api.scan()
            self.assertEqual(len(result["markets"]), 1)
            self.assertEqual(calls[-1][1]["offset"], 100)
        finally:
            await api.close()

    async def test_repair_after_delta_and_duplicate(self):
        class FakeAPI:
            calls = 0
            async def book(self, asset):
                self.calls += 1
                e = full("102")
                e["asset_id"] = asset
                return e
        with tempfile.TemporaryDirectory() as d:
            s = Store(str(Path(d) / "test.sqlite"))
            api = FakeAPI()
            r = Recorder(api, s, [fixture()[2]])
            await r.process(full(), 1000)
            delta = {"event_type": "price_change", "market": "condition", "timestamp": "101", "price_changes": [{"asset_id": "a", "side": "BUY", "price": "0.4", "size": "2"}]}
            await r.process(delta, 1001)
            self.assertEqual(api.calls, 1)
            self.assertTrue(r.books["a"].synced)
            flags = json.loads(s.db.execute("SELECT flags FROM snapshots WHERE event_type='price_change'").fetchone()[0])
            self.assertIn("unverified_continuity", flags)
            await r.process(delta, 1002)
            self.assertEqual(api.calls, 1)
            s.close()

    async def test_identity_mismatch_repaired(self):
        class FakeAPI:
            async def book(self, asset):
                e = full("103")
                e["asset_id"] = asset
                return e
        with tempfile.TemporaryDirectory() as d:
            s = Store(str(Path(d) / "test.sqlite"))
            r = Recorder(FakeAPI(), s, [fixture()[2]])
            e = full()
            e["market"] = "wrong"
            await r.process(e, 1000)
            self.assertEqual(r.books["a"].exchange_ms, 103)
            self.assertEqual(s.db.execute("SELECT count(*) FROM raw_events").fetchone()[0], 2)
            s.close()

    async def test_rediscovery_changes_subscription_and_status(self):
        market = fixture()[2]
        class FakeAPI:
            async def scan(self):
                return {"markets": [market], "status": "ok"}
        with tempfile.TemporaryDirectory() as d:
            s = Store(str(Path(d) / "test.sqlite"))
            r = Recorder(FakeAPI(), s, [market])
            self.assertFalse(await r.rediscover())
            market["outcomes"][1]["asset_id"] = "c"
            self.assertTrue(await r.rediscover())
            self.assertEqual(set(r.books), {"a", "c"})
            s.close()


if __name__ == "__main__":
    unittest.main()
