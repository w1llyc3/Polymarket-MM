import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal, getcontext, localcontext
from pathlib import Path
from unittest.mock import patch

import httpx

from nba_recorder.storage import encode
from nba_recorder.fair_value.demo import AT, market, quote, observation, create_demo
from nba_recorder.fair_value.engine import Config, evaluate, market_at, books
from nba_recorder.fair_value.matcher import match_event
from nba_recorder.fair_value.maths import implied_probability, proportional_devig, consensus
from nba_recorder.fair_value.markouts import HORIZONS, markout, update_markouts
from nba_recorder.fair_value.providers import JSONProvider, OddsBatch, ProviderError, TheOddsAPIProvider, odds_api_quotes
from nba_recorder.fair_value.store import FairStore, read_connection
from nba_recorder.teams import team_code


class MathsTests(unittest.TestCase):
    def test_american_conversion(self):
        for odds, probability in [(100, "0.5"), (-100, "0.5"), (200, str(Decimal(1)/3)), (-200, str(Decimal(2)/3)), (300, "0.25")]:
            with self.subTest(odds=odds):
                self.assertAlmostEqual(float(implied_probability(odds, "american")), float(probability), places=15)

    def test_formats_equivalent(self):
        self.assertEqual(implied_probability(-150, "american"), implied_probability(Decimal(5)/3, "decimal").quantize(Decimal("0.1")))
        self.assertEqual(proportional_devig(200, 200, "american")["home"], proportional_devig(3, 3, "decimal")["home"])

    def test_symmetric_vig_and_underround(self):
        for a, b, fmt in [(-110, -110, "american"), ("1.91", "1.91", "decimal"), ("2.1", "2.1", "decimal")]:
            fair = proportional_devig(a, b, fmt)
            self.assertEqual(Decimal(fair["home"]), Decimal("0.5"))
            self.assertEqual(Decimal(fair["away"]), Decimal("0.5"))
        self.assertLess(Decimal(proportional_devig("2.1", "2.1", "decimal")["overround"]), 0)

    def test_asymmetric_proportional_formula(self):
        p = proportional_devig(-200, 180, "american")
        with localcontext() as ctx:
            ctx.prec = 42
            self.assertEqual(Decimal(p["home"]), Decimal(28)/43)
            self.assertEqual(Decimal(p["home"]) + Decimal(p["away"]), 1)

    def test_invalid_odds_matrix(self):
        for value, fmt in [(0,"american"),(99,"american"),(-99,"american"),("-110.5","american"),(1,"decimal"),(0,"decimal"),(-2,"decimal"),(True,"decimal"),("NaN","decimal"),("Infinity","american"),(None,"decimal"),("garbage","decimal"),(2,"unknown")]:
            with self.subTest(value=value, fmt=fmt), self.assertRaises(ValueError):
                proportional_devig(value, 2, fmt)

    def test_reproducible_independent_of_decimal_context(self):
        a = proportional_devig(-123, 141, "american")
        old = getcontext().prec
        try:
            getcontext().prec = 6
            self.assertEqual(a, proportional_devig(-123, 141, "american"))
        finally:
            getcontext().prec = old

    def test_consensus_and_dispersion(self):
        result = consensus(["0.6", "0.4"])
        self.assertEqual(Decimal(result["home"]), Decimal("0.5"))
        self.assertEqual(Decimal(result["stddev"]), Decimal("0.1"))
        self.assertEqual(Decimal(result["range"]), Decimal("0.2"))
        self.assertEqual(result, consensus(["0.4", "0.6"]))


class MatcherTests(unittest.TestCase):
    def test_negated_market_rules_never_classified_as_compatible(self):
        for phrase in ["not including any overtime", "excluding overtime", "overtime not included"]:
            m = market()
            m["description"] = m["description"].replace("including any overtime periods", phrase)
            self.assertIn("unknown_or_mismatched_settlement_rules", match_event(m, quote(), AT))

    def test_aliases_and_exact_roles(self):
        self.assertEqual(team_code(" LA Clippers "), "lac")
        self.assertIsNone(team_code("Los Angeles"))
        self.assertEqual(match_event(market(), quote(), AT), [])
        q = quote()
        q["home_team"], q["away_team"] = q["away_team"], q["home_team"]
        self.assertIn("team_or_home_away_mismatch", match_event(market(), q, AT))

    def test_start_and_rules_matrix(self):
        for change, expected in [("start", "start_time_mismatch"), ("overtime", "unknown_or_mismatched_settlement_rules"), ("cancellation", "unknown_or_mismatched_settlement_rules"), ("postponement", "unknown_or_mismatched_settlement_rules")]:
            q = quote()
            if change == "start":
                q["start_ms"] += 1
            elif change == "overtime":
                q["rules"][change] = False
            else:
                q["rules"][change] = "void"
            self.assertIn(expected, match_event(market(), q, AT))

    def test_unknown_rules_mapping_and_foreign_venue(self):
        m = market()
        m["description"] = "No documented rules"
        self.assertIn("unknown_or_mismatched_settlement_rules", match_event(m, quote(), AT))
        m = market()
        m["outcomes"][0]["label"] = "Yes"
        self.assertIn("unverified_asset_team_mapping", match_event(m, quote(), AT))
        m = market()
        m["venue"] = "polymarket.us"
        self.assertIn("market_not_eligible", match_event(m, quote(), AT))

    def test_live_cancelled_and_future_rule_evidence(self):
        for status in ["live", "cancelled", "ended"]:
            q = quote()
            q["status"] = status
            self.assertIn("not_pregame_or_cancelled", match_event(market(), q, AT))
        q = quote()
        q["rule_evidence"]["verified_ms"] = AT + 1
        self.assertIn("missing_rule_evidence", match_event(market(), q, AT))


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "data.sqlite")
        self.store = FairStore(self.path)
        self.store.base.markets([market()], AT - 5000)
        self.store.base.health(AT - 5000, "connected")
        observation(self.store, AT - 100)
        observation(self.store, AT - 100, asset="synthetic-away")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def ingest(self, quotes=None, received=AT-500, provider="test"):
        quotes = quotes if quotes is not None else [quote(), quote("synthetic-book-b", "2", "2", "decimal")]
        return self.store.ingest(OddsBatch(provider, received, {"quotes": quotes}, quotes, "synthetic"))

    def first(self, at=AT, config=Config()):
        return evaluate(self.store.db, "synthetic-demo-market", at, config)[0]

    def test_no_data_is_no_quote(self):
        s = self.first()
        self.assertEqual(s["status"], "NO_QUOTE")
        self.assertIn("no_valid_reference", s["reasons"])
        self.assertEqual(s["candidates"], [])

    def test_consensus_edges_and_traceability(self):
        raw = self.ingest()
        s = self.first()
        self.assertEqual(s["status"], "RESEARCH_SIGNAL")
        self.assertEqual(Decimal(s["fair_probability"]), Decimal("0.5"))
        self.assertEqual(Decimal(s["buy_gross_edge"]), Decimal("0.05"))
        self.assertEqual(Decimal(s["sell_gross_edge"]), Decimal("0.05"))
        self.assertEqual(Decimal(s["buy_taker_gross_edge"]), Decimal("-0.05"))
        self.assertEqual(Decimal(s["sell_taker_gross_edge"]), Decimal("-0.05"))
        self.assertEqual(Decimal(s["consensus_deviation"]), 0)
        self.assertEqual(s["reference_age_ms"], 1000)
        self.assertFalse(s["auto_live_quote_allowed"])
        self.assertTrue(s["reference_eligible"])
        self.assertEqual(len(s["candidates"]), 2)
        self.assertEqual(s["sources"][0]["raw_odds_id"], raw)
        self.assertIsNotNone(s["snapshot_id"])
        self.assertEqual(s, self.first())
        self.store.persist(s)
        self.store.persist(s)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM fv_signals").fetchone()[0], 1)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM fv_candidates").fetchone()[0], 2)

    def test_single_source_research_only(self):
        self.ingest([quote()])
        s = self.first()
        self.assertEqual(s["status"], "RESEARCH_ONLY")
        self.assertFalse(s["reference_eligible"])
        self.assertFalse(s["auto_live_quote_allowed"])

    def test_stale_future_and_invalid_odds(self):
        for change in ["stale", "future", "invalid"]:
            q = quote()
            if change == "stale": q["updated_ms"] = AT - 120001
            elif change == "future": q["updated_ms"] = AT + 1
            else: q["home_odds"] = "NaN"
            self.ingest([q], received=AT - 400 + ["stale","future","invalid"].index(change))
            self.assertEqual(self.first()["status"], "NO_QUOTE")

    def test_team_mismatch_and_rules_mismatch(self):
        q = quote()
        q["home_team"] = "Miami Heat"
        self.ingest([q])
        self.assertEqual(self.first()["status"], "NO_QUOTE")
        q = quote()
        q["rules"]["cancellation"] = "void"
        self.ingest([q], AT - 300)
        self.assertEqual(self.first()["status"], "NO_QUOTE")

    def test_conflicting_sources(self):
        self.ingest([quote(), quote("synthetic-book-b", -900, 900)])
        s = self.first()
        self.assertEqual(s["source_count"], 2)
        self.assertIn("source_conflict", s["reasons"])
        self.assertEqual(s["status"], "NO_QUOTE")
        self.assertEqual(s["candidates"], [])

    def test_one_live_cancelled_source_vetoes_whole_event(self):
        for index, status in enumerate(["live", "cancelled"]):
            q = quote()
            q["status"] = status
            self.ingest([q, quote("synthetic-book-b")], AT - 300 + index)
            self.assertEqual(self.first()["status"], "NO_QUOTE")

    def test_pregame_to_live_boundary(self):
        self.ingest()
        m = market()
        m["event_live"] = True
        self.store.base.markets([m], AT)
        self.assertEqual(self.first()["status"], "NO_QUOTE")
        self.assertEqual(self.first(AT + 3600000)["status"], "NO_QUOTE")

    def test_duplicate_book_across_providers_counts_once(self):
        self.ingest([quote()], provider="feed-a")
        self.ingest([quote()], received=AT - 400, provider="feed-b")
        s = self.first()
        self.assertEqual(s["source_count"], 1)
        self.assertEqual(s["status"], "RESEARCH_ONLY")
        self.assertEqual(sum(e["included"] for e in s["sources"]), 1)

    def test_disappeared_event_does_not_use_previous_batch(self):
        self.ingest()
        self.ingest([], AT - 200)
        self.assertEqual(self.first()["status"], "NO_QUOTE")

    def test_future_odds_books_metadata_do_not_change_past_signal(self):
        self.ingest()
        before = self.first()
        q = quote(home=-900, away=900, updated=AT+10000)
        self.ingest([q], AT + 11000)
        observation(self.store, AT + 10000, "0.8", "0.9")
        m = market()
        m["description"] = "Future invalid rules"
        self.store.base.markets([m], AT + 12000)
        self.assertEqual(before, self.first())

    def test_late_received_old_quote_not_known_in_past(self):
        self.ingest([quote(updated=AT-10000)], received=AT + 1)
        self.assertEqual(self.first()["status"], "NO_QUOTE")

    def test_future_exchange_timestamp_not_used(self):
        self.ingest()
        s = {"market_id": "synthetic-demo-market", "asset_id": "synthetic-home", "exchange_timestamp": AT + 1, "local_receive_timestamp": AT,
             "bid": "0.9", "ask": "0.99", "flags": [], "bids": [], "asks": []}
        self.store.base.commit({"event_type":"book", "timestamp":str(AT+1)}, "test", AT, [s])
        self.assertIn("future_exchange_timestamp", self.first()["reasons"])

    def test_disconnected_or_unverified_books(self):
        self.ingest()
        observation(self.store, AT, flags=["unverified_continuity"])
        self.assertEqual(self.first()["status"], "NO_QUOTE")
        observation(self.store, AT+1)
        self.store.base.health(AT+1, "disconnected")
        self.assertIn("no_connected_session_asof", self.first(AT+1)["reasons"])

    def test_no_asof_metadata_never_reads_latest_market(self):
        self.ingest()
        self.store.db.execute("DELETE FROM market_versions")
        self.store.db.commit()
        self.assertEqual(self.first()["reasons"], ["missing_asof_market_metadata"])

    def test_duplicate_snapshot_observation_retains_verification_time(self):
        original = books(self.store.db, "synthetic-demo-market", "synthetic-home", end=AT)[-1]
        event = json.loads(self.store.db.execute("SELECT payload FROM raw_events WHERE event_key=?", (original["event_key"],)).fetchone()[0])
        s = original["payload"].copy()
        s["local_receive_timestamp"] = AT
        self.assertFalse(self.store.base.commit(event, "synthetic", AT, [s]))
        latest = books(self.store.db, "synthetic-demo-market", "synthetic-home", end=AT)[-1]
        self.assertEqual(latest["observed_ms"], AT)
        self.assertEqual(latest["snapshot_id"], original["snapshot_id"])

    def test_markout_horizons_and_buy_sell_sign(self):
        self.ingest()
        s = self.first()
        self.store.persist(s)
        candidates = {c["side"]: c for c in s["candidates"]}
        for horizon in HORIZONS:
            self.assertEqual(markout(self.store.db, candidates["BUY"], horizon, AT)["status"], "PENDING")
            observation(self.store, AT + horizon*1000, "0.50", "0.60")
            b = markout(self.store.db, candidates["BUY"], horizon, AT + horizon*1000)
            sell = markout(self.store.db, candidates["SELL"], horizon, AT + horizon*1000)
            self.assertEqual(Decimal(b["mid_markout"]), Decimal("0.10"))
            self.assertEqual(Decimal(sell["mid_markout"]), Decimal("0.00"))
            self.assertEqual(Decimal(sell["gross_liquidation_markout"]), Decimal("-0.05"))
            self.assertTrue(b["not_profit"])
        update_markouts(self.store, AT+305000)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM fv_markouts").fetchone()[0], 8)
        historical = update_markouts(self.store, AT)
        self.assertTrue(all(m["status"] == "PENDING" for m in historical))
        before = self.first()
        update_markouts(self.store, AT+400000)
        self.assertEqual(before, self.first())

    def test_markout_window_bad_book_and_started_game(self):
        self.ingest()
        candidate = self.first()["candidates"][0]
        self.assertEqual(markout(self.store.db,candidate,10,AT+16000)["status"], "UNAVAILABLE")
        observation(self.store, AT+30000, flags=["crossed"])
        observation(self.store, AT+30001)
        self.assertEqual(markout(self.store.db,candidate,30,AT+31000)["status"], "UNAVAILABLE")
        m = market()
        m["event_live"] = True
        self.store.base.markets([m], AT+59000)
        observation(self.store, AT+60000)
        self.assertEqual(markout(self.store.db,candidate,60,AT+60000)["status"], "UNAVAILABLE")

    def test_rule_timestamp_and_synthetic_live_mixing(self):
        self.ingest()
        m = market()
        m.pop("synthetic")
        self.store.base.markets([m], AT-50)
        self.assertEqual(self.first()["status"], "NO_QUOTE")


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_json_receipt_cannot_be_backdated(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/"odds.json"
            q = quote()
            q["received_ms"] = 1
            p.write_text(encode({"schema_version":1,"data_kind":"synthetic","quotes":[q]}),encoding="utf-8")
            batch = await JSONProvider(p).fetch()
            self.assertGreater(batch.received_ms, AT)

    async def test_missing_key_no_request(self):
        with patch.dict(os.environ,{"ODDS_API_KEY":""}):
            with self.assertRaises(ProviderError):
                await TheOddsAPIProvider().fetch()

    async def test_http_get_and_key_not_persisted_or_error_leaked(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200,json=[])
        with patch.dict(os.environ,{"ODDS_API_KEY":"synthetic-secret-not-real"}):
            batch = await TheOddsAPIProvider(transport=httpx.MockTransport(handler)).fetch()
            self.assertEqual(requests[0].method,"GET")
            self.assertEqual(requests[0].url.host,"api.the-odds-api.com")
            self.assertNotIn("synthetic-secret",encode(batch.raw))
            def denied(request): return httpx.Response(401,json={"error":"forbidden"})
            with self.assertRaises(ProviderError) as error:
                await TheOddsAPIProvider(transport=httpx.MockTransport(denied)).fetch()
            self.assertNotIn("synthetic-secret",str(error.exception))
            self.assertIn("401",str(error.exception))

    async def test_secret_fields_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"bad.json"
            for name in ["private_key", "wallet_private_key", "PRIVATE KEY", "api_key", "signing-key"]:
                p.write_text(encode({"schema_version":1,"data_kind":"recorded","quotes":[],name:"not-a-real-key"}),encoding="utf-8")
                with self.assertRaises(ProviderError): await JSONProvider(p).fetch()

    async def test_api_mapper_timestamp_market_type_and_unknown_rules(self):
        raw = [{"id":"game","sport_key":"basketball_nba","commence_time":"2026-10-20T19:00:00Z","home_team":"Detroit Pistons","away_team":"Boston Celtics",
                "bookmakers":[{"key":"book","last_update":"2026-10-14T05:00:00Z","markets":[{"key":"spreads","outcomes":[]},
                  {"key":"h2h","outcomes":[{"name":"Detroit Pistons","price":-110},{"name":"Boston Celtics","price":-110}]}]}]}]
        quotes = odds_api_quotes(raw,"american",AT,{})
        self.assertEqual(len(quotes),1)
        self.assertEqual(quotes[0]["source_id"],"sportsbook:book")
        self.assertEqual(quotes[0]["updated_ms"],AT)
        self.assertEqual(quotes[0]["rules"],{})


class DemoTests(unittest.TestCase):
    def test_missing_key_cli_no_quote_nonzero_exit(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"ODDS_API_KEY":""}):
            path = str(Path(d)/"missing-key.sqlite")
            result = subprocess.run([sys.executable,"-m","nba_recorder.fair_value","ingest","--db",path,"--provider","the-odds-api"],capture_output=True,text=True)
            self.assertEqual(result.returncode,1)
            output=json.loads(result.stdout)
            self.assertTrue(all(s["status"]=="NO_QUOTE" for s in output["signals"]))
            self.assertIn("no request made",output["collections"][0]["message"])

    def test_read_only_cli_and_replay(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d)/"cli.sqlite")
            original = create_demo(path)
            before = Path(path).read_bytes()
            result = subprocess.run([sys.executable,"-m","nba_recorder.fair_value","evaluate","--db",path,"--at-ms",str(AT)],capture_output=True,text=True,check=True)
            replay = json.loads(result.stdout)
            self.assertEqual(replay, original["signals"])
            subprocess.run([sys.executable,"-m","nba_recorder.fair_value","show","--db",path],capture_output=True,text=True,check=True)
            self.assertEqual(before, Path(path).read_bytes())

    def test_end_to_end_demo_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            path=str(Path(d)/"demo.sqlite")
            output=create_demo(path)
            self.assertEqual(output["data_kind"],"synthetic")
            self.assertEqual(len(output["markouts"]),16)
            self.assertTrue(all(m["status"]=="OBSERVED" and m["not_profit"] for m in output["markouts"]))
            with self.assertRaises(ValueError): create_demo(path)
            db=read_connection(path)
            with self.assertRaises(Exception): db.execute("DELETE FROM fv_signals")
            db.close()


if __name__ == "__main__":
    unittest.main()
