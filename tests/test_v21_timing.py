"""Availability regressions created before repairing v2 freeze."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from test_core import fixture, full
from nba_recorder.book import Book
from nba_recorder.storage import Store
from nba_recorder.quote_engine.storage import PaperStore
from nba_recorder.inplay.tape import freeze
from nba_recorder.inplay.demo import AT


class AvailabilityRegressions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.source_path = Path(self.tmp.name)/"source.sqlite"
        self.source = Store(self.source_path)
        self.tape = PaperStore(Path(self.tmp.name)/"tape.sqlite")
        self.sports_path = Path(self.tmp.name)/"sports.sqlite"
        self.sports = sqlite3.connect(self.sports_path)
        self.sports.execute("CREATE TABLE sports_raw(id INTEGER PRIMARY KEY,receive_ms INTEGER,processed_ms INTEGER,payload TEXT)")
        _, _, market = fixture()
        market.update(event_live=True, synthetic=True)
        self.source.markets([market], AT)
        self.source.health(AT, "connected")
        raw = full(str(AT+10))
        book = Book(market, "a")
        book.apply(raw, AT+10)
        with patch("nba_recorder.storage.time.time_ns", return_value=(AT+100)*1000000):
            self.source.commit(raw, "ws", AT+10, [book.snapshot(AT+10, 30)])
        trade = {"event_type": "last_trade_price", "timestamp": str(AT+150), "market": "condition", "asset_id": "a", "side": "SELL", "price": ".39", "size": "2"}
        with patch("nba_recorder.storage.time.time_ns", return_value=(AT+1000)*1000000):
            self.source.commit(trade, "ws", AT+200, [])
        sports = {"gameId": "g", "leagueAbbreviation": "NBA", "homeTeam": "Pistons", "awayTeam": "Celtics", "status": "InProgress", "live": True, "ended": False, "period": "Q4", "elapsed": "02:00", "upstream_ms": AT+250, "paused": False}
        self.sports.execute("INSERT INTO sports_raw VALUES(1,?,?,?)", (AT+300, AT+1100, json.dumps(sports)))
        self.sports.commit()
        contract = {"game_id": "g", "event_id": "1", "home_team": "det", "away_team": "bos", "identity_evidence": "synthetic fixture", "clock_semantics": "REMAINING_IN_PERIOD", "clock_evidence": "fixture only", "source_timestamp_ms_field": "upstream_ms", "timestamp_evidence": "fixture only", "pause_field": "paused", "pause_evidence": "fixture only"}
        self.identity = freeze(self.tape, self.source_path, AT, AT+1200, self.sports_path, {"2": contract}, step_ms=257)
        self.events = [json.loads(r[0]) for r in self.tape.db.execute("SELECT payload FROM qe_input WHERE tape_id=? ORDER BY seq", (self.identity,))]

    def tearDown(self):
        self.source.close()
        self.sports.close()
        self.tape.close()
        self.tmp.cleanup()

    def test_trade_first_available_at_processed_time_preserves_exchange_time(self):
        found = [(e["time_ms"], t) for e in self.events for t in e["trades"]]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0][0], AT+1000)
        self.assertEqual(found[0][1]["exchange_ms"], AT+150)

    def test_sports_not_visible_until_processed_and_bucket_is_present(self):
        visible = [e["time_ms"] for e in self.events if any(f["sports"].get("game_id") == "g" for f in e["frames"])]
        self.assertTrue(visible)
        self.assertEqual(min(visible), AT+1100)

    def test_book_remains_unavailable_before_processing(self):
        visible = [e["time_ms"] for e in self.events if any(f.get("book") for f in e["frames"])]
        self.assertEqual(min(visible), AT+100)

    def refreeze(self):
        key = freeze(self.tape, self.source_path, AT, AT+1200, self.sports_path, step_ms=257)
        return key, [json.loads(r[0]) for r in self.tape.db.execute("SELECT payload FROM qe_input WHERE tape_id=? ORDER BY seq", (key,))]

    def test_metadata_and_connection_completion_are_not_receive_times(self):
        with self.source.db:
            self.source.db.execute("UPDATE market_versions SET processed_ms=?", (AT+450,))
            self.source.db.execute("UPDATE health SET processed_ms=?", (AT+600,))
        _, events = self.refreeze()
        self.assertEqual(min(e["time_ms"] for e in events if e["frames"]), AT+450)
        self.assertEqual(min(e["time_ms"] for e in events if any(f["connected"] for f in e["frames"])), AT+600)

    def test_missing_processing_is_unknown_and_future_append_does_not_change_tape(self):
        with self.source.db:
            self.source.db.execute("UPDATE health SET processed_ms=NULL")
        first, events = self.refreeze()
        self.assertFalse(any(f["connected"] for e in events for f in e["frames"]))
        self.source.health(AT+2000, "connected")
        self.source.markets([json.loads(self.source.db.execute("SELECT metadata FROM market_versions LIMIT 1").fetchone()[0])], AT+2000)
        self.sports.execute("INSERT INTO sports_raw VALUES(2,?,?,?)", (AT+2000, None, '{}'))
        self.sports.commit()
        second, repeated = self.refreeze()
        self.assertEqual(first, second)
        self.assertEqual(events, repeated)
