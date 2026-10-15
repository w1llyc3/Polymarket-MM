import asyncio
import http.client
import json
import tempfile
import threading
import unittest
from types import SimpleNamespace
from pathlib import Path
from nba_recorder.console.service import ConsoleService
from nba_recorder.console.__main__ import make_server


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = ConsoleService(self.tmp.name)
        self.server = make_server(self.service, 0)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.request(method, path, body, headers or {})
            response = connection.getresponse()
            return response.status, response.read(), dict(response.getheaders())
        finally:
            connection.close()

    def test_host_origin_token_and_content_type_protection(self):
        status, raw, _ = self.request("GET", "/api/bootstrap")
        self.assertEqual(status, 200)
        token = json.loads(raw)["token"]
        body = json.dumps({"request_id": "id", "session_id": None, "expected_version": self.service.version, "action": "scan"})
        good = {"Origin": "http://127.0.0.1:"+str(self.port), "X-Console-Token": token, "Content-Type": "application/json"}
        for bad in ({}, {**good, "Origin": "https://evil.example"}, {**good, "Host": "evil.example"}, {**good, "X-Console-Token": "wrong"}):
            self.assertEqual(self.request("POST", "/api/command", body, bad)[0], 403)
        self.assertEqual(self.request("POST", "/api/command", body, {**good, "Content-Type": "text/plain"})[0], 400)
        self.assertEqual(self.request("POST", "/api/command", body, good)[0], 202)
        self.assertEqual(self.service.commands.qsize(), 1)

    def test_read_only_refresh_empty_unknown_and_no_wildcard_cors(self):
        for _ in range(3):
            status, raw, headers = self.request("GET", "/api/state")
            self.assertEqual(status, 200)
            view = json.loads(raw)
            self.assertEqual(view["state"], {})
            self.assertFalse(view["live_permission"])
            self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertEqual(list((Path(self.tmp.name)/"data"/"console").glob("*.sqlite")), [])
        self.assertEqual(self.request("GET", "/api/state", headers={"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.request("GET", "/../../.env")[0], 404)

    def test_idempotent_http_commands_and_collision(self):
        cmd = {"request_id": "same", "session_id": None, "expected_version": self.service.version, "action": "start", "mode": "SYNTHETIC_DEMO", "confirmed": True}
        self.service.submit(cmd)
        self.service.submit(cmd)
        self.assertEqual(self.service.commands.qsize(), 1)
        with self.assertRaises(ValueError):
            self.service.submit({**cmd, "action": "stop"})

    def test_different_ports_cannot_start_second_console_owner(self):
        second = ConsoleService(self.tmp.name)
        self.service.start()
        try:
            with self.assertRaisesRegex(RuntimeError, "已有实例"):
                second.start()
        finally:
            self.service.close()

    def test_ui_resume_cannot_override_unknown_or_closed_phase(self):
        from nba_recorder.inplay.store import SingleWriterStore
        from nba_recorder.inplay.manager import UnifiedPaperManager
        from nba_recorder.inplay.runner import PaperRunner
        from nba_recorder.inplay.demo import AT, frame
        for phase in ("UNKNOWN", "MM_CLOSED"):
            store = SingleWriterStore(Path(self.tmp.name)/(phase+".sqlite"))
            try:
                om = UnifiedPaperManager(store, start_ms=AT)
                runner = PaperRunner(om)
                f = frame(AT, period=4, remaining=60) if phase == "MM_CLOSED" else frame(AT)
                if phase == "UNKNOWN":
                    f["sports"] = {}
                cooked = runner.session.observe(f)
                runner.latest = {"a": cooked}
                om.kill("test_boundary")
                om.reconcile()
                self.service.runner = runner
                self.service.task = SimpleNamespace(done=lambda: False)
                self.service.lifecycle = "RUNNING"
                self.service.mode = "REAL_PAPER"
                with self.assertRaises(ValueError):
                    self.service.handle({"session_id": None, "expected_version": self.service.version, "action": "resume", "confirmed": True})
                self.assertTrue(om.halted)
                self.assertEqual(om.phases.get("m")["state"], phase)
                self.assertEqual(om.real_submission_calls, 0)
            finally:
                self.service.runner = None
                self.service.task = None
                store.close()


class OwnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = ConsoleService(self.tmp.name)
        self.owner = asyncio.create_task(self.service.serve())

    async def asyncTearDown(self):
        self.service.shutdown_requested.set()
        await self.owner
        self.tmp.cleanup()

    async def command(self, action, **extra):
        view = self.service.read()
        command = {"request_id": __import__('uuid').uuid4().hex, "session_id": view["session_id"], "expected_version": view["version"], "action": action, "confirmed": True, **extra}
        self.service.submit(command)
        for _ in range(100):
            await asyncio.sleep(.02)
            receipt = self.service.receipt(command["request_id"])
            if receipt["status"] != "PENDING":
                return receipt
        self.fail("owner acknowledgement timeout")

    async def test_single_owner_start_pause_stop_export_and_read_projection(self):
        receipt = await self.command("start", mode="SYNTHETIC_DEMO")
        self.assertEqual(receipt["status"], "APPLIED")
        await asyncio.sleep(.15)
        runner = self.service.runner
        self.assertIsNotNone(runner)
        duplicate = await self.command("start", mode="SYNTHETIC_DEMO")
        self.assertEqual(duplicate["status"], "REJECTED")
        self.assertIs(self.service.runner, runner)
        count = runner.om.db.execute("SELECT count(*) FROM l6_events WHERE kind='CAPITAL'").fetchone()[0]
        for _ in range(10):
            self.service.read()
        self.assertEqual(runner.om.db.execute("SELECT count(*) FROM l6_events WHERE kind='CAPITAL'").fetchone()[0], count)
        pause = await self.command("pause")
        self.assertEqual(pause["status"], "APPLIED")
        self.assertFalse(runner.quotes_enabled)
        risks = runner.om.db.execute("SELECT count(*) FROM pe_risks").fetchone()[0]
        self.assertEqual(self.service.submit(pause["command"])["status"], "APPLIED")
        self.assertEqual(runner.om.db.execute("SELECT count(*) FROM pe_risks").fetchone()[0], risks)
        await asyncio.sleep(.5)
        self.assertGreater(runner.ticks, 1)  # no browser heartbeat needed
        self.assertEqual((await self.command("resume"))["status"], "REJECTED")
        stop = await self.command("stop")
        self.assertEqual(stop["status"], "APPLIED")
        await self.service.task
        view = self.service.read()
        self.assertEqual(view["lifecycle"], "STOPPED")
        self.assertEqual(view["state"]["risk_state"], "HALTED")
        self.assertTrue(self.service.final_report["ledger_and_oms_consistent"])
        export = await self.command("export")
        self.assertEqual(export["status"], "APPLIED")
        identity = export["result"]["download"].rsplit("/", 1)[1]
        self.assertTrue(self.service.exports[identity].exists())
        self.assertEqual(self.service.final_report["real_submission_calls"], 0)

    async def test_stale_session_version_rejected_and_no_silent_mode_upgrade(self):
        with self.assertRaises(ValueError):
            self.service.handle({"session_id": "wrong", "expected_version": self.service.version, "action": "start", "mode": "SYNTHETIC_DEMO"})
        result = await self.command("start", mode="REAL_PAPER", market_id="invented")
        self.assertEqual(result["status"], "REJECTED")
        self.assertIsNone(self.service.runner)
