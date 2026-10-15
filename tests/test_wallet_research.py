import json
import tempfile
import unittest
from pathlib import Path
import httpx
from nba_recorder.wallet_research.collector import paginate, IncompleteWindow, amount_stats, collect_wallet

ADDRESS = "0x"+"1"*40


class WalletResearchTests(unittest.TestCase):
    def test_window_bisection_keeps_equal_timestamp_boundaries(self):
        rows = [{"timestamp": stamp, "id": str(stamp)} for stamp in range(1, 9)]
        def fetch(path, params):
            subset = [r for r in rows if params["start"] <= r["timestamp"] <= params["end"]]
            return subset[params["offset"]:params["offset"]+params["limit"]]
        result = paginate(fetch, "/activity", {}, 1, 8, limit=2, cap=0)
        self.assertEqual(result, rows)

    def test_same_second_overflow_and_outside_window_fail_explicitly(self):
        with self.assertRaises(IncompleteWindow):
            paginate(lambda p, q: [{"timestamp": 1}]*2, "/activity", {}, 1, 1, 2, 0)
        with self.assertRaises(IncompleteWindow):
            paginate(lambda p, q: [{"timestamp": 9}], "/activity", {}, 1, 2)

    def test_amounts_are_dollars_not_shares(self):
        stats = amount_stats([{"amount": "4", "shares": "100"}, {"amount": "12"}])
        self.assertEqual(stats["median_dollars"], "8")
        self.assertEqual(stats["le_5_count_fraction"], "0.5")
        self.assertEqual(stats["le_5_amount_fraction"], "0.25")

    def handler(self, requests):
        def handle(request):
            requests.append(request)
            if request.url.path == "/public-profile":
                return httpx.Response(200, json={"proxyWallet": ADDRESS, "name": "fixture"})
            if request.url.path == "/sports":
                return httpx.Response(200, json=[{"sport": "nba", "series": "10345"}])
            if request.url.path.startswith("/events/slug/"):
                return httpx.Response(200, json={"id": "E", "title": "NBA preseason", "series": [{"id": "10345"}], "markets": [{"id": "M", "conditionId": "C", "sportsMarketType": "moneyline", "slug": "nba-bos-det-2026-10-14", "clobTokenIds": '["a","b"]'}]})
            trade = {"proxyWallet": ADDRESS, "timestamp": 1791954001, "type": "TRADE", "eventSlug": "nba-bos-det-2026-10-14", "conditionId": "C", "asset": "a", "side": "BUY", "price": ".4", "size": "10", "usdcSize": "4", "transactionHash": "not-an-order-id"}
            return httpx.Response(200, json=[trade])
        return handle

    def test_explicit_taker_false_roles_unknown_and_metadata_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            requests = []
            out = Path(tmp)/"evidence"
            summary = collect_wallet(ADDRESS, out, 1791954000, 1791954010, httpx.MockTransport(self.handler(requests)))
            self.assertTrue(summary["complete_window"])
            self.assertEqual(summary["nba_statistics"]["median_dollars"], "4")
            self.assertEqual(summary["role_known_coverage"], "0")
            self.assertIsNone(summary["known_maker_fraction"])
            self.assertEqual(next(r for r in requests if r.url.path == "/trades").url.params["takerOnly"], "false")
            self.assertIn("UNKNOWN", (out/"nba_fills.csv").read_text())
            self.assertTrue(all(p["sha256"] and p["http_status"] == 200 for p in json.loads((out/"requests.json").read_text())))

    def test_http_failure_is_blocked_not_empty_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            summary = collect_wallet(ADDRESS, Path(tmp)/"evidence", 1791954000, 1791954010, httpx.MockTransport(lambda r: httpx.Response(503)))
            self.assertEqual(summary["status"], "BLOCKED")
            self.assertFalse(summary["complete_window"])
            self.assertIsNone(summary["nba_statistics"])
