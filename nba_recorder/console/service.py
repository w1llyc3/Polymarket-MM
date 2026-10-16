"""One asyncio owner, one runner, serialized idempotent operator commands."""
import asyncio
import copy
import json
import queue
import subprocess
import threading
import os
import time
import uuid
from pathlib import Path
from ..api import PublicAPI, utc_ms
from ..fair_value.store import digest
from ..inplay.demo import AT, events
from ..inplay.manager import UnifiedPaperManager
from ..inplay.runner import PaperRunner, run_paper
from ..inplay.report import report
from ..inplay.store import SingleWriterStore
from ..paper_execution.risk import ACTIVE
from .state import snapshot


class ConsoleService:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.data = self.root/"data"/"console"
        self.data.mkdir(parents=True, exist_ok=True)
        probe = self.data/("permission-"+uuid.uuid4().hex)
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        try:
            self.commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.root, text=True, stderr=subprocess.DEVNULL).strip()
            self.dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=self.root, text=True, stderr=subprocess.DEVNULL).strip())
        except (OSError, subprocess.SubprocessError):
            self.commit = "UNKNOWN"
            self.dirty = None
        self.lock = threading.Lock()
        self.commands = queue.Queue(maxsize=100)
        self.receipts = {}
        self.exports = {}
        self.view = {}
        self.runner = None
        self.task = None
        self.scan_task = None
        self.session_id = None
        self.mode = None
        self.session_deadline_ms = None
        self.session_started_ms = None
        self.limit_s = 14400
        self.postgame_s = 600
        self.bound_market = None
        self.lifecycle = "IDLE"
        self.version = 0
        self.catalog = {"status": "EMPTY", "markets": [], "last_success_ms": None, "error": None}
        self.final_report = None
        self.last_snapshot = {}
        self.last_publish = 0
        self.last_signature = None
        self.shutdown_requested = threading.Event()
        self.thread = None
        self.instance_lock = None
        self.publish(force=True)

    def start(self):
        self.instance_lock = open(self.data/"console.instance.writer.lock", "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.instance_lock.write(b"0")
                self.instance_lock.flush()
                self.instance_lock.seek(0)
                msvcrt.locking(self.instance_lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self.instance_lock.close()
            raise RuntimeError("同一仓库操作台已有实例，请勿在其他端口重复启动") from error
        def owned():
            try:
                asyncio.run(self.serve())
            finally:
                self.instance_lock.close()
        self.thread = threading.Thread(target=owned, name="paper-owner", daemon=False)
        self.thread.start()

    def read(self):
        with self.lock:
            return copy.deepcopy(self.view)

    def submit(self, command):
        allowed = {"request_id", "session_id", "expected_version", "action", "mode", "market_id", "confirmed", "discovery_scope", "season_type", "day_filter", "limit_s", "postgame_s"}
        if not isinstance(command, dict) or set(command)-allowed or not isinstance(command.get("request_id"), str) or not 1 <= len(command["request_id"]) <= 100:
            raise ValueError("命令格式错误")
        identity = command["request_id"]
        with self.lock:
            old = self.receipts.get(identity)
            if old:
                if old["command"] != command:
                    raise ValueError("幂等键不能用于不同命令")
                return copy.deepcopy(old)
            if len(self.receipts) >= 10000:
                raise ValueError("命令预算已用尽，请停止会话后重启操作台")
            receipt = {"request_id": identity, "status": "PENDING", "received_ms": utc_ms(), "command": copy.deepcopy(command)}
            self.receipts[identity] = receipt
            try:
                self.commands.put_nowait(copy.deepcopy(command))
            except queue.Full:
                receipt.update(status="REJECTED", reason="命令队列已满")
            return copy.deepcopy(receipt)

    def receipt(self, identity):
        with self.lock:
            return copy.deepcopy(self.receipts.get(identity))

    def attach(self, runner):
        self.runner = runner
        self.lifecycle = "RUNNING"
        self.version += 1
        self.publish(force=True)

    async def pump(self):
        # Called by run_paper on its owning loop; never constructs a second OMS.
        self.publish()

    def finished(self, result):
        self.final_report = result
        self.publish(force=True)

    def begin_stop(self):
        self.lifecycle = "STOPPING"
        if self.runner:
            self.runner.quotes_enabled = False
        self.version += 1
        self.publish(force=True)

    def publish(self, force=False):
        if not force and time.monotonic()-self.last_publish < .5:
            return
        if self.runner:
            self.last_snapshot = snapshot(self)
        signature = digest([self.lifecycle, self.session_id, self.last_snapshot.get("risk_state"), [p.get("state") for p in self.last_snapshot.get("phases", [])]])
        if signature != self.last_signature:
            self.version += 1
            self.last_signature = signature
        value = {"app": "NBA_MM_LOCAL_CONSOLE_V2_2", "version": self.version, "session_id": self.session_id,
                 "lifecycle": self.lifecycle, "mode": self.mode, "commit": self.commit, "config_version": "BookPolicy-v2.2",
                 "working_tree_dirty_at_start": self.dirty,
                 "server_ms": utc_ms(), "catalog": self.catalog,
                 "runtime": {"limit_s": self.limit_s, "started_ms": self.session_started_ms, "deadline_ms": self.session_deadline_ms, "postgame_s": self.postgame_s,
                             "expiry_action": "停止采集、请求撤单、核对并保持 HALTED；剩余库存保留旧账本，不自动平仓",
                             "synthetic": self.mode == "SYNTHETIC_DEMO"}, "state": self.last_snapshot,
                 "live_permission": False, "manual_confirmation": "只读；本版不在网页续期或写入人工授权",
                 "acceptance": {"Data": "BOUNDED_ONLY", "Offline_Paper": "CONTROLLED", "Real_input_Observation": "CONTROLLED", "Quoting": "NOT_VERIFIED", "Unattended": "NO_GO", "Live": "NO_GO"}}
        with self.lock:
            self.view = copy.deepcopy(value)
        self.last_publish = time.monotonic()

    @staticmethod
    def runnable(market):
        status = market.get("status", {})
        event_status = market.get("event_status", {})
        return (event_status.get("active") is not False and not any(event_status.get(k) for k in ("closed", "archived"))
                and not market.get("event_ended") and all(status.get(k) for k in ("active", "enableOrderBook", "acceptingOrders"))
                and not any(status.get(k) for k in ("closed", "archived", "tracking_state")))

    async def scan(self, discovery_scope="CURRENT", season_type="REGULAR_SEASON", day_filter="WINDOW"):
        if discovery_scope not in {"CURRENT", "HISTORY"}:
            raise ValueError("unsupported discovery scope")
        now = utc_ms()
        start, end = (now-86400000, now+7*86400000) if discovery_scope == "CURRENT" else (now-7*86400000, now)

        if day_filter in {"TODAY", "TOMORROW"}:
            # UTC+8 local calendar boundaries, never exchange slug dates.
            local_day = (now+8*3600000)//86400000 + (day_filter == "TOMORROW")
            start, end = local_day*86400000-8*3600000, (local_day+1)*86400000-8*3600000
        elif day_filter != "WINDOW":
            raise ValueError("unsupported day filter")
        self.catalog = {**self.catalog, "status": "LOADING", "error": None}
        self.publish(force=True)
        api = PublicAPI()
        try:
            kwargs = {"scope": "full-game", "start_ms": start, "end_ms": end}
            if season_type != "REGULAR_SEASON":
                kwargs["season_type"] = season_type
            result = await asyncio.wait_for(api.scan(**kwargs), 60)
            markets = []
            for m in result["markets"]:
                ended = bool(m.get("event_ended") or m["status"].get("closed") or m["status"].get("archived"))
                state = "ENDED" if ended else "LIVE" if m.get("event_live") else "SCHEDULED" if m["game_start_ms"] > now else "UNKNOWN"
                if discovery_scope == "CURRENT" and not self.runnable(m):
                    continue
                markets.append({**m, "discovery_state": state, "discovered_ms": utc_ms(),
                                "read_only": discovery_scope == "HISTORY" or not self.runnable(m)})
            self.catalog = {"status": "READY", "markets": markets, "last_success_ms": utc_ms(), "error": None,
                            "discovery_scope": discovery_scope, "season_type": season_type, "day_filter": day_filter, "unclassified_markets": result.get("unclassified_markets", []), "window_start_ms": start, "window_end_ms": end}
        except Exception as error:
            self.catalog = {**self.catalog, "status": "ERROR", "error": "真实扫描失败："+type(error).__name__+"；检查网络后重试，旧列表不代表实时"}
        finally:
            await api.close()
            self.publish(force=True)

    def handle(self, cmd):
        if cmd.get("session_id") != self.session_id or type(cmd.get("expected_version")) is not int or cmd["expected_version"] != self.version:
            raise ValueError("会话或状态版本已变化，请刷新状态后重新确认")
        action = cmd.get("action")
        if action == "scan":
            if cmd.get("season_type", "REGULAR_SEASON") not in {"REGULAR_SEASON", "PRESEASON"} or cmd.get("day_filter", "WINDOW") not in {"WINDOW", "TODAY", "TOMORROW"}:
                raise ValueError("unsupported season/date filter")
            if cmd.get("discovery_scope", "CURRENT") not in {"CURRENT", "HISTORY"}:
                raise ValueError("unsupported discovery scope")
            if self.scan_task and not self.scan_task.done():
                return {"message": "扫描已在进行"}
            self.scan_task = asyncio.create_task(self.scan(cmd.get("discovery_scope", "CURRENT"), cmd.get("season_type", "REGULAR_SEASON"), cmd.get("day_filter", "WINDOW")))
            return {"message": "已开始真实扫描，等待列表结果"}
        if action == "start":
            if self.task and not self.task.done():
                raise ValueError("已有会话，禁止启动第二个 Runner")
            mode = cmd.get("mode")
            if mode not in {"OBSERVATION", "REAL_PAPER", "SYNTHETIC_DEMO"}:
                raise ValueError("模式不可用；离线回放请使用已有开发 CLI")
            if not cmd.get("confirmed"):
                raise ValueError("启动需要明确确认数据与执行模式")
            market = None
            if mode != "SYNTHETIC_DEMO":
                if self.catalog["status"] != "READY" or utc_ms()-(self.catalog["last_success_ms"] or 0) > 60000:
                    raise ValueError("请先刷新真实比赛列表，列表有效期60秒")
                market = next((m for m in self.catalog["markets"] if m["market_id"] == cmd.get("market_id")), None)
                if not market:
                    raise ValueError("比赛不在当前真实扫描结果中")
                if market.get("read_only") or not self.runnable(market):
                    raise ValueError("历史或结束/无效市场只读，禁止新启动")
            from ..pilot import PilotWindow
            limit_s, postgame_s = cmd.get("limit_s", 14400), cmd.get("postgame_s", 600)
            PilotWindow(utc_ms(), limit_s, postgame_s)  # validate before allocating any capital
            self.limit_s, self.postgame_s, self.bound_market = limit_s, postgame_s, market
            self.session_started_ms = utc_ms()
            self.session_deadline_ms = self.session_started_ms+limit_s*1000 if mode != "SYNTHETIC_DEMO" else None
            self.session_id = uuid.uuid4().hex
            self.mode, self.lifecycle = mode, "STARTING"
            self.runner, self.final_report, self.last_snapshot = None, None, {}
            self.version += 1
            self.task = asyncio.create_task(self.session(market))
            return {"message": "会话已分配，等待后台状态", "session_id": self.session_id}
        if not self.runner or not self.task or self.task.done():
            if action == "export" and self.session_id:
                return self.export()
            raise ValueError("当前无运行中的会话")
        om = self.runner.om
        if self.lifecycle != "RUNNING" and action != "export":
            raise ValueError("会话正在停止或异常；禁止在清理期间恢复/修改账户")
        if action in {"pause", "stop", "reconcile", "resume", "stop_paper"} and cmd.get("confirmed") is not True:
            raise ValueError("此操作需要明确确认")
        if action == "stop_paper":
            self.runner.stop_paper("console_operator_stop_paper_keep_recording")
            self.version += 1
            return {"message": "Paper已停止并请求撤单；公开采集继续，库存保留，本会话不再恢复报价"}
        if action == "pause":
            self.runner.quotes_enabled = False
            om.pause("console_operator_pause")
            self.version += 1
            return {"message": "已暂停新增报价并请求撤销活动单；库存未清空", "risk_state": om.risk_state}
        if action == "stop":
            self.runner.quotes_enabled = False
            om.kill("console_operator_stop")
            self.runner.stopping.set()
            self.lifecycle = "STOPPING"
            self.version += 1
            return {"message": "撤单请求已生效，等待回报和核对；尚未宣称清仓", "stop_request_applied": True}
        if action == "reconcile":
            result = om.reconcile()
            self.version += 1
            return {"message": "已执行权威 Paper 核对；未自动恢复", "consistent": result, "risk_state": om.risk_state}
        if action == "resume":
            if self.runner.paper_stopped:
                raise ValueError("本会话Paper已明确停止；公开采集继续，不恢复报价")
            if self.runner.game_information and self.runner.game_information.get("game_finished"):
                raise ValueError("比赛结束后仅继续采集，不恢复报价")
            if self.mode == "OBSERVATION":
                raise ValueError("观察模式不能升级为报价模式")
            if not self.runner.latest:
                raise ValueError("缺少有效行情")
            assets = {f["asset_id"] for f in self.runner.latest.values()}
            required = {o["asset_id"] for f in self.runner.latest.values() for o in f["market"]["outcomes"]}
            if assets != required:
                raise ValueError("需要双资产完整新鲜行情后才能恢复")
            from ..inplay.reference import book_risk
            # Atomic on the owner loop: never temporarily enable submission.
            for frame in self.runner.latest.values():
                candidate = {**self.runner.authorize(frame), "phase": om.phases.boundary(frame, om.now)}
                if book_risk(candidate, om.policy, om.now, check_operator=False):
                    raise ValueError("阶段、数据或市场安全门未通过；不能人工跳过 UNKNOWN/MM_CLOSED")
            if not om.resume():
                raise ValueError("恢复被原有账户风控拒绝，请先核对")
            self.runner.quotes_enabled = True
            self.version += 1
            return {"message": "后台风险检查通过，允许受控 Paper；不代表清仓或实盘许可"}
        if action == "export":
            return self.export()
        raise ValueError("未知命令")

    def export(self):
        self.publish(force=True)
        identity = uuid.uuid4().hex
        bundle = {"session_id": self.session_id, "commit": self.commit, "exported_ms": utc_ms(), "view": self.read(),
                  "completed_report": self.final_report, "scope": "completed report" if self.final_report else "partial live projection; bounded log tail", "real_submission_calls": 0, "database_files": {"paper": self.session_id+".sqlite", "public_source": self.session_id+"-source.sqlite"}}
        path = self.data/(self.session_id+"-"+identity+".json")
        path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
        with self.lock:
            self.exports[identity] = path
        return {"message": "报告已保存", "download": "/api/export/"+identity, "session_id": self.session_id}

    async def session(self, market):
        try:
            path = self.data/self.session_id
            if self.mode == "SYNTHETIC_DEMO":
                await self.demo(path)
            else:
                result = await run_paper(str(path)+".sqlite", str(path)+"-source.sqlite", market["slug"], market["market_id"], seconds=self.limit_s, control=self, research=market.get("season_type") == "PRESEASON", postgame_s=self.postgame_s)
                self.final_report = result
            self.lifecycle = "STOPPED"
        except Exception as error:
            self.lifecycle = "FAILED"
            self.last_snapshot = {**self.last_snapshot, "fatal_error": "后台会话异常："+type(error).__name__+"。需要人工检查日志与账本，不能视为停止成功"}
        finally:
            self.runner = None
            self.version += 1
            self.publish(force=True)

    async def demo(self, path):
        store = SingleWriterStore(str(path)+".sqlite")
        runner = None
        try:
            om = UnifiedPaperManager(store, start_ms=AT)
            runner = PaperRunner(om, "L1")
            self.attach(runner)
            stream = events()
            om.split(stream[0]["frames"][0]["market"], "5", "explicit-synthetic-console-split")
            # Advance the same deterministic fixture, not invented real time.
            # Real wall watchdog remains active but maps to fixture clock only.
            runner.last_wall_ms = None
            async def demo_watchdog():
                while not runner.stopping.is_set():
                    runner.tick(om.now)
                    await asyncio.sleep(.1)
            watchdog = asyncio.create_task(demo_watchdog())
            runner.watchdog_task = watchdog
            try:
                await asyncio.sleep(0)  # first independent heartbeat before data
                for event in stream:
                    if runner.stopping.is_set():
                        break
                    frames = [runner.authorize(f) for f in event["frames"]]
                    runner.latest.update({f["asset_id"]: f for f in frames})
                    runner.session.on_event({**event, "frames": frames})
                    runner.events += 1
                    self.publish(force=True)
                    await asyncio.sleep(1)
            finally:
                self.begin_stop()
                runner.stopping.set()
                await watchdog
                om.kill("console_demo_ended")
                om.advance(om.now+om.policy.request_timeout_ms+20)
                om.reconcile()
                om.kill("console_demo_stopped_keep_halted")
                result = report(om)
                result.update(source="SYNTHETIC_DEMO", console_session_id=self.session_id, not_real_profit=True)
                from ..inplay.__main__ import save_report
                save_report(om, result)
                self.finished(result)
        finally:
            store.close()

    async def serve(self):
        try:
            while not self.shutdown_requested.is_set():
                try:
                    cmd = self.commands.get_nowait()
                except queue.Empty:
                    await asyncio.sleep(.05)
                    self.publish()
                    continue
                try:
                    value = self.handle(cmd)
                    status = "APPLIED"
                except Exception as error:
                    value = {"message": str(error) if isinstance(error, ValueError) else "后台命令失败："+type(error).__name__}
                    status = "REJECTED"
                if self.runner:
                    with self.runner.om.db:
                        self.runner.om._log("CONSOLE_COMMAND_RECEIPT", detail={"request_id": cmd["request_id"], "session_id": self.session_id, "expected_version": cmd["expected_version"], "action": cmd.get("action"), "status": status, "result": value})
                with (self.data/"commands.jsonl").open("a", encoding="utf-8") as audit:
                    audit.write(json.dumps({"time_ms": utc_ms(), "request_id": cmd["request_id"], "session_id": self.session_id, "action": cmd.get("action"), "status": status, "result": value}, ensure_ascii=False)+"\n")
                with self.lock:
                    self.receipts[cmd["request_id"]].update(status=status, result=value, applied_ms=utc_ms(), version=self.version)
                self.publish(force=True)
        finally:
            if self.runner:
                self.runner.quotes_enabled = False
                self.runner.om.kill("console_server_shutdown")
                self.runner.stopping.set()
            if self.task and not self.task.done():
                await self.task
            if self.scan_task and not self.scan_task.done():
                self.scan_task.cancel()
                await asyncio.gather(self.scan_task, return_exceptions=True)

    def close(self):
        self.shutdown_requested.set()
        if self.thread:
            self.thread.join(timeout=35)
