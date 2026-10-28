import asyncio
import unittest
from unittest.mock import Mock
from websockets.client import ClientProtocol
from websockets.uri import parse_uri
from websockets.asyncio.server import serve
from nba_recorder.ws_transport import EarlyDisconnectConnection, connect
from nba_recorder.game_state import information
from nba_recorder.pilot import PilotWindow


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_early_tls_reset_completes_cleanup(self):
        conn = EarlyDisconnectConnection(ClientProtocol(parse_uri("wss://example.com")))
        error = ConnectionResetError("injected before connection_made")
        conn.connection_lost(error)
        self.assertTrue(conn.connection_lost_waiter.done())
        self.assertIs(conn.recv_exc, error)
        self.assertTrue(conn.recv_messages.closed)

    async def test_normal_connection_and_reconnect(self):
        async def handler(ws):
            await ws.send("snapshot")
            await ws.close()
        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            for _ in range(2):
                async with connect(f"ws://127.0.0.1:{port}", proxy=None) as ws:
                    self.assertEqual(await ws.recv(), "snapshot")


class EndTests(unittest.TestCase):
    def setUp(self):
        self.market = dict(polymarket_game_id=12, home_team="bos", away_team="chi")
        self.raw = dict(gameId=12, leagueAbbreviation="nba", homeTeam="BOS",
                        awayTeam="CHI", ended=True, live=False, status="closed", period="VFT")

    def test_observed_closed_starts_tail_without_quote_permission(self):
        info = information(self.raw, self.market, 1000, 1000)
        self.assertTrue(info["game_finished"])
        self.assertEqual(info["game_phase"], "ENDED")
        self.assertFalse(info["quote_authorized"])
        w = PilotWindow(0, 27000, 1800)
        w.observe(info, 1000)
        w.observe(information(self.raw, self.market, 2000, 2000), 2000)
        self.assertEqual(w.finished_ms, 1000)
        self.assertIsNone(w.stop_reason(1800999))
        self.assertEqual(w.stop_reason(1801000), "OBSERVED_GAME_END_PLUS_TAIL")

    def test_invalid_end_evidence_does_not_stop(self):
        for change in ({"ended":False}, {"ended":"true"}, {"live":True},
                       {"gameId":13}, {"homeTeam":"CHI"}, {"awayTeam":"BOS"},
                       {"leagueAbbreviation":"nfl"}, {"status":"Canceled"},
                       {"status":"InProgress"}):
            with self.subTest(change=change):
                self.assertFalse(information(self.raw | change,self.market,1000,1000)["game_finished"])
        w = PilotWindow(0,27000,1800)
        w.observe(information(self.raw,self.market,1000,1000),20000)
        self.assertIsNone(w.finished_ms)

    def test_existing_final_and_overtime(self):
        for status in ("Final", "F/OT"):
            self.assertTrue(information(self.raw | {"status":status},self.market,1000,1000)["game_finished"])
