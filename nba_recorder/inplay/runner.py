"""One-game real-input Paper runner; same BookSession/OMS/FIFO as replay.

Only public REST snapshots and public market/sports WS. Async source tasks never write
the paper account. An independent wall-clock watchdog owns the same session,
even while HTTP is pending or no message is delivered.
"""
import asyncio
import json
import time
from pathlib import Path
from websockets.asyncio.client import connect
from ..api import PublicAPI, GAMMA, WS, utc_ms, select_market
from ..book import Book
from ..fair_value.store import digest
from ..storage import Store, encode
from .manager import UnifiedPaperManager
from .policy import BookPolicy
from .session import BookSession
from .store import SingleWriterStore
from .sports import SPORTS_WS, normalize
from .report import report


class PaperRunner:
    def __init__(self, manager, model="L2", watchdog_ms=250, manual_file=None):
        if not 10 <= watchdog_ms <= 1000:
            raise ValueError("watchdog interval must be 10..1000 ms")
        self.om, self.session = manager, BookSession(manager, model)
        self.watchdog_ms, self.manual_file = watchdog_ms, manual_file
        self.latest = {}
        self.sequence = 0
        self.stopping = asyncio.Event()
        self.ticks, self.events = 0, 0
        self.issues = []
        self.last_wall_ms = None
        self.connection_guard = None
        self.connection_epoch = None
        self.watchdog_task = None
        self.last_watchdog_tick_mono = None
        self.quotes_enabled = True
        self.game_information = None
        self.bound_market = None
        self.paper_stopped = self.om.db.execute("SELECT 1 FROM ip_session_config WHERE id='pilot_paper_stopped'").fetchone() is not None
        if self.paper_stopped:
            self.quotes_enabled = False
        self.om.runtime_guard = self.authorize

    def stop_paper(self, reason):
        with self.om.db:
            self.om.db.execute("INSERT OR IGNORE INTO ip_session_config VALUES('pilot_paper_stopped',?)", (encode({"reason": reason, "time_ms": self.om.now}),))
            self.paper_stopped = True
            self.quotes_enabled = False
            self.om.kill(reason)

    def observe_game_information(self, info, window, at):
        if getattr(self, '_last_chart_game', None) != info:
            with self.om.db:
                self.om.db.execute("INSERT INTO ip_game_observations(time_ms,payload) VALUES(?,?)", (at, encode({**info,"observer_available_ms":at})))
            self._last_chart_game = info
        self.game_information = info
        window.observe(info, at)
        if window.finished_ms is not None:
            with self.om.db:
                self.om.db.execute("INSERT OR IGNORE INTO ip_session_config VALUES('recording_finished_ms',?)",(str(window.finished_ms),))
        if window.finished_ms is not None and not self.paper_stopped:
            self.stop_paper("observed_game_finished_keep_recording_tail")

    def authorize(self, frame):
        authorized=self.om.db.execute("SELECT 1 FROM ip_session_config WHERE id='research_authorization'").fetchone()
        if authorized:
            frame={**frame,'market':{**frame['market'],'research_authorized':True}}
        observed = frame.get("observed_connected", frame.get("connected"))
        epoch = self.connection_epoch() if self.connection_epoch else None
        source_epoch = frame.get("connection_epoch")
        watchdog_age = time.monotonic()-self.last_watchdog_tick_mono if self.last_watchdog_tick_mono is not None else None
        healthy = self.watchdog_task is None or (not self.watchdog_task.done() and watchdog_age is not None and watchdog_age <= max(1, 3*self.watchdog_ms/1000))
        connected = bool(observed and healthy and
                         (self.connection_guard is None or self.connection_guard()) and
                         (epoch is None or source_epoch == epoch) and
                         (frame.get("message_epoch", epoch) == epoch))
        return {**frame, "observed_connected": observed, "connected": connected,
                "runtime_permission": {"transport_now": self.connection_guard() if self.connection_guard else None,
                                       "physical_connected": bool(observed and (self.connection_guard is None or self.connection_guard())),
                                       "epoch_matches": (epoch is None or source_epoch == epoch) and frame.get("message_epoch", epoch) == epoch,
                                       "account_new_risk": not self.om.halted,
                                       "epoch_now": epoch, "source_epoch": source_epoch,
                                       "watchdog_alive": healthy, "watchdog_age_ms": int(watchdog_age*1000) if watchdog_age is not None else None, "quotes_enabled": self.quotes_enabled}}

    def manual_update(self, at):
        if not self.om.policy.allow_manual_phase or not self.manual_file:
            return
        try:
            grant = json.loads(Path(self.manual_file).read_text(encoding="utf-8-sig"))
            key = grant["confirmation_id"]
            old = self.om.db.execute("SELECT payload FROM ip_manual_confirmations WHERE id=?", (key,)).fetchone()
            if old:
                stored = json.loads(old[0])
                stored.pop("strategy_available_ms", None)
                if stored != grant:
                    raise ValueError("manual confirmation ID reused")
                return
            from .manual import permission
            market = next((f["market"] for f in self.latest.values() if f["market_id"] == grant.get("market_id")), None)
            if not market:
                return
            _, errors = permission(grant, market, at, self.om.policy.manual_max_age_ms)
            if errors:
                raise ValueError(";".join(errors))
            with self.om.db:
                saved = {**grant, "strategy_available_ms": at}
                self.om.db.execute("INSERT INTO ip_manual_confirmations VALUES(?,?,?)", (key, at, encode(saved)))
                self.om._log("MANUAL_CONFIRMED_NOT_AUTOMATIC", detail=saved)
        except (OSError, ValueError, KeyError, TypeError):
            with self.om.db:
                self.om._risk("manual_confirmation_unavailable_or_invalid")
                if self.om.db.execute("SELECT 1 FROM ip_manual_confirmations LIMIT 1").fetchone() or any(o["state"] in {"OPEN", "PARTIALLY_FILLED", "SUBMITTING", "QUEUED"} for o in self.om.orders()):
                    self.om._kill("manual_confirmation_invalid")

    def feed(self, frames, trades=(), provenance="PUBLIC_INPUT", reuse=False, message_epoch=None):
        at = max(utc_ms(), self.om.now)
        self.sequence += 1
        cooked = []
        for frame in frames:
            received = frame.get("received_ms")
            cooked.append(self.authorize({**frame, "time_ms": at, "strategy_available_ms": at,
                           **({"message_epoch": message_epoch} if message_epoch is not None else {}),
                           "source_processed_ms": frame.get("processed_ms"), "processed_ms": frame.get("processed_ms") if reuse else at,
                           "backlog_ms": frame.get("backlog_ms") if reuse else at-received if type(received) is int else None,
                           "input_provenance": provenance}))
        self.latest.update({f["asset_id"]: f for f in cooked})
        self.manual_update(at)
        event = {"id": digest(["live-paper-v2.1", self.om.run_id, at, self.sequence, cooked, trades]),
                 "time_ms": at, "frames": cooked, "trades": list(trades), "provenance": provenance}
        self.session.on_event(event)
        self.events += 1

    def tick(self, wall_ms=None):
        self.last_watchdog_tick_mono = time.monotonic()
        wall = utc_ms() if wall_ms is None else wall_ms
        if self.last_wall_ms is not None and wall < self.last_wall_ms:
            self.om.kill("wall_clock_regression")
        self.last_wall_ms = wall
        at = max(self.om.now, wall)
        self.manual_update(at)
        self.sequence += 1
        frames = [self.authorize({**f, "time_ms": at}) for f in self.latest.values()]
        self.session.on_event({"id": digest(["watchdog-v2.1", self.om.run_id, at, self.sequence]), "time_ms": at,
                               "kind": "WATCHDOG", "wall_ms": wall, "frames": frames, "trades": [], "provenance": "WALL_CLOCK_WATCHDOG"})
        self.ticks += 1

    async def watchdog(self):
        while not self.stopping.is_set():
            self.tick()
            try:
                await asyncio.wait_for(self.stopping.wait(), self.watchdog_ms/1000)
            except asyncio.TimeoutError:
                pass


async def run_paper(db_path, source_path, event_slug, market_id, seconds=30, interval=1, model="L2", manual_file=None, contracts=None, operator_reset=False, control=None, research=False, postgame_s=600, initial_market=None, filter_sports_to_game=False):
    if seconds <= 0 or seconds > 28800 or interval <= 0 or Path(db_path).resolve() == Path(source_path).resolve():
        raise ValueError("bounded duration, positive interval and distinct source/paper paths required")
    api = PublicAPI()
    store = SingleWriterStore(db_path)
    source = Store(source_path)
    now = utc_ms()
    saved = store.db.execute("SELECT config FROM pe_control WHERE id=1").fetchone()
    parameters={'quote_distance':'.03' if getattr(control,'paper_plan','SMALL')=='WIDE' else '.02',**getattr(control,'research_overrides',{})}
    policy = BookPolicy(**json.loads(saved[0])) if saved else BookPolicy(allow_manual_phase=manual_file is not None, research_phase=control is not None,
                initial_cash=getattr(control,'paper_budget','100'),**parameters)
    if policy.allow_manual_phase != (manual_file is not None):
        source.close()
        store.close()
        await api.close()
        raise ValueError("phase authority immutable: select same automatic/manual mode")
    om = UnifiedPaperManager(store, policy, now)
    # Bind one explicit game before any network request. No silent switching.
    binding = encode({"market_id": str(market_id), "event_slug": event_slug})
    bound = om.db.execute("SELECT payload FROM ip_session_config WHERE id='runner_game'").fetchone()
    if bound and bound[0] != binding:
        source.close()
        store.close()
        await api.close()
        raise ValueError("one explicit game per paper database")
    with om.db:
        om.db.execute("INSERT OR IGNORE INTO ip_session_config VALUES('runner_game',?)", (binding,))
    runner = PaperRunner(om, model, manual_file=manual_file)
    if control:
        runner.quotes_enabled = control.mode != "OBSERVATION" and not runner.paper_stopped
        control.attach(runner)
    runner.issues.append("AUTO_SPORTS_CONTRACT_UNVERIFIED" if not manual_file else "MANUAL_CONFIRMED_NOT_UNATTENDED")
    if operator_reset:
        om.reconcile()
        om.resume()  # explicit CLI operator action, never default restart recovery
    queue = asyncio.Queue(maxsize=8)
    runner.input_queue=queue
    market_state = {}
    sports_state = {}
    from ..pilot import PilotWindow
    from ..game_state import information
    window = PilotWindow(now, max(60, min(28800, int(seconds))), postgame_s)
    saved_end=om.db.execute("SELECT payload FROM ip_session_config WHERE id='recording_finished_ms'").fetchone()
    if saved_end:
        window.finished_ms=int(saved_end[0])
        if window.finished_ms>now:raise ValueError('future recorded end')
    stop_reason = "HARD_LIMIT_NOT_COMPLETE"
    with source.db:
        source.db.executescript("CREATE TABLE IF NOT EXISTS game_information(id INTEGER PRIMARY KEY,received_ms INTEGER,processed_ms INTEGER,payload TEXT); CREATE TABLE IF NOT EXISTS public_metadata(id INTEGER PRIMARY KEY,received_ms INTEGER,payload TEXT);")
    health = {"rest_batches": 0, "sports_updates": 0, "metadata_updates": 0, "rest_errors": 0, "sports_connections": 0, "market_ws_connections": 0, "public_trades": 0}
    market_connection = {"connected": False, "epoch": 0}
    runner.connection_guard = lambda: market_connection["connected"]
    runner.connection_epoch = lambda: market_connection["epoch"]
    deadline = time.monotonic()+seconds

    async def metadata():
        while time.monotonic() < deadline:
            try:
                sports = await api.get(GAMMA, "/sports")
                nba = next(s for s in sports if s.get("sport") == "nba")
                event = await api.get(GAMMA, "/events/slug/"+event_slug)
                raw = next(m for m in event["markets"] if str(m.get("id")) == str(market_id))
                if initial_market and initial_market.get("discovery_schema"):
                    from ..discovery import normalize_market
                    selected, reason = normalize_market(event, raw, nba, utc_ms(), initial_market.get("official_game"))
                else:
                    selected, reason = select_market(event, raw, str(nba["series"]), utc_ms(), nba.get("ordering"), scope="full-game", research=research)
                if not selected or not selected.get("home_away_evidence"):
                    raise ValueError("market identity/type/season/roles failed: "+reason)
                if initial_market:
                    invariant = ('market_id','condition_id','event_id','home_team','away_team','outcomes','rules_verified','game_start_ms','version','season_type')
                    if any(selected.get(k) != initial_market.get(k) for k in invariant):
                        raise ValueError('selected identity/rules changed; explicit confirmation required')
                    selected['research_authorized'] = bool(initial_market.get('research_authorized'))
                at = utc_ms()
                if market_state.get("resolved"):
                    selected["status"]["closed"] = True
                runner.bound_market = selected
                with source.db:
                    source.db.execute("INSERT INTO public_metadata(received_ms,payload) VALUES(?,?)", (at, encode(event)))
                if runner.game_information is None:
                    info = information({"gameId": event.get("gameId"), "leagueAbbreviation": "NBA",
                                        "homeTeam": selected.get("home_team"), "awayTeam": selected.get("away_team"),
                                        "score": event.get("score"), "period": event.get("period"),
                                        "elapsed": event.get("elapsed"), "status": event.get("status"), "ended": event.get("ended")}, selected, at, at)
                    info.update(source=GAMMA+"/events/slug/"+event_slug, identity_evidence="Gamma event team roles; metadata only", game_finished=False)
                    runner.game_information = info
                source.markets([selected], at, processed_ms=at)
                market_state.update(market=selected, received_ms=at, processed_ms=at)
                # Game ended is independent of market closed/resolved. Exact
                # Gamma game identity/roles have just been revalidated above.
                if event.get('ended') is True and selected.get('polymarket_game_id') is not None and str(event.get('gameId'))==str(selected['polymarket_game_id']):
                    market_state['end_info']={'display_eligible':True,'conflict':False,'game_finished':True,
                        'received_ms':at,'processed_ms':at,'source':GAMMA+'/events/slug/'+event_slug,
                        'identity_evidence':'revalidated exact Gamma game ID and team roles; event ended flag, not market closure'}
                health["metadata_updates"] += 1
            except asyncio.CancelledError:
                raise
            except Exception as error:
                health["rest_errors"] += 1
                runner.issues.append("METADATA_"+type(error).__name__)
                source.health(utc_ms(), "disconnected", "metadata_unavailable")
                if isinstance(error, ValueError):
                    om.kill("runner_metadata_identity_or_rules_error")
                else:
                    with om.db:
                        om._risk('metadata_transport_unavailable', evidence={'type':type(error).__name__})
            await asyncio.sleep(10)

    async def books_loop():
        while time.monotonic() < deadline:
            if "market" not in market_state:
                await asyncio.sleep(.1)
                continue
            market = json.loads(encode(market_state["market"]))
            observed = market_state["processed_ms"]
            try:
                rows = []
                epoch = market_connection["epoch"]
                for outcome in market["outcomes"]:
                    raw = await api.book(outcome["asset_id"])
                    received = utc_ms()
                    raw = {**raw, "event_type": "rest_snapshot"}
                    if str(raw.get("asset_id")) != outcome["asset_id"] or raw.get("market") != market["condition_id"]:
                        raise ValueError("REST book identity mismatch")
                    book = Book(market, outcome["asset_id"])
                    book.apply(raw, received)
                    snapshot = book.snapshot(received, policy.book_max_age_ms/1000)
                    key = source.key(raw, "rest")
                    source.commit(raw, "rest", received, [snapshot], key)
                    processed = utc_ms()
                    rows.append({"strategy_mode": "BOOK_DRIVEN", "asset_id": outcome["asset_id"], "market_id": str(market_id), "market": market,
                                 "market_observed_ms": observed, "market_available_ms": observed, "time_ms": processed,
                                 "book": snapshot, "book_received_ms": received, "book_ref": key+":"+str(received), "connected": True,
                                 "received_ms": received, "processed_ms": processed, "sports": {}})
                    rows[-1]["connection_epoch"] = epoch
                source.health(utc_ms(), "connected", "REST_PUBLIC_SNAPSHOTS_NOT_WS_CONTINUITY")
                await queue.put(rows)
                health["rest_batches"] += 1
            except asyncio.CancelledError:
                raise
            except Exception as error:
                health["rest_errors"] += 1
                runner.issues.append("BOOK_"+type(error).__name__)
                source.health(utc_ms(), "disconnected", "book_transport_or_schema_failure")
            await asyncio.sleep(interval)

    async def market_ws_loop():
        while time.monotonic() < deadline:
            if "market" not in market_state:
                await asyncio.sleep(.1)
                continue
            m = market_state["market"]
            assets = {o["asset_id"] for o in m["outcomes"]}
            try:
                async with connect(WS, ping_interval=None, open_timeout=10, close_timeout=2) as ws:
                    await ws.send(encode({"type": "market", "assets_ids": sorted(assets), "custom_feature_enabled": True}))
                    market_connection["connected"] = True
                    market_connection["epoch"] += 1
                    ws_epoch = market_connection["epoch"]
                    health["market_ws_connections"] += 1
                    source.health(utc_ms(), "connected", "market_ws_epoch_"+str(ws_epoch))
                    last_pong = time.monotonic()
                    next_ping = last_pong+10
                    while time.monotonic() < deadline:
                        if time.monotonic() >= next_ping:
                            await ws.send("PING")
                            next_ping = time.monotonic()+10
                        if time.monotonic()-last_pong > 30:
                            raise TimeoutError("market heartbeat")
                        try:
                            raw = await asyncio.wait_for(ws.recv(), 1)
                        except asyncio.TimeoutError:
                            continue
                        received = utc_ms()
                        if raw == "PONG":
                            last_pong = time.monotonic()
                            continue
                        messages = json.loads(raw)
                        for msg in messages if isinstance(messages, list) else [messages]:
                            if not isinstance(msg, dict) or msg.get("market") != m["condition_id"]:
                                continue
                            key = source.key(msg, "ws")
                            if source.contains(key):
                                continue
                            source.commit(msg, "ws", received, [], key)
                            processed = utc_ms()
                            if msg.get("event_type") == "last_trade_price" and msg.get("asset_id") in assets:
                                try:
                                    trade = {"event_id": key, "asset_id": msg["asset_id"], "condition_id": msg["market"], "side": msg.get("side"), "price": msg["price"], "size": msg.get("size"),
                                             "identity_basis":"full public event fingerprint; identical unidentifiable prints may coalesce",
                                             "exchange_ms": int(msg["timestamp"]), "received_ms": received, "processed_ms": processed, "strategy_available_ms": processed}
                                except (ValueError, KeyError, TypeError):
                                    runner.issues.append("PUBLIC_TRADE_SCHEMA_UNKNOWN")
                                    continue
                                await queue.put({"trade": trade, "connection_epoch": ws_epoch})
                                health["public_trades"] += 1
                            elif msg.get("event_type") == "market_resolved":
                                market_state["resolved"] = True
                                market_state["market"]["status"]["closed"] = True
                                source.markets([market_state["market"]], processed)
                                om.kill("official_market_resolved")
            except asyncio.CancelledError:
                raise
            except Exception as error:
                market_connection["connected"] = False
                runner.issues.append("MARKET_WS_"+type(error).__name__)
                source.health(utc_ms(), "disconnected", "market_ws_gap")
                await asyncio.sleep(1)
            finally:
                market_connection["connected"] = False

    async def sports_loop():
        source.db.execute("CREATE TABLE IF NOT EXISTS sports_raw(id INTEGER PRIMARY KEY,receive_ms INTEGER,processed_ms INTEGER,payload TEXT)")
        while time.monotonic() < deadline:
            try:
                async with connect(SPORTS_WS, ping_interval=None, open_timeout=10, close_timeout=2) as ws:
                    health["sports_connections"] += 1
                    source.health(utc_ms(), "connected", "sports_ws_information_only")
                    while time.monotonic() < deadline:
                        raw = await ws.recv()
                        received = utc_ms()
                        if raw == "ping":
                            await ws.send("pong")
                            continue
                        payload = json.loads(raw)
                        processed = utc_ms()
                        if filter_sports_to_game and (not isinstance(payload,dict) or str(payload.get('gameId'))!=str((runner.bound_market or initial_market or {}).get('polymarket_game_id'))):
                            continue
                        with source.db:
                            source.db.execute("INSERT INTO sports_raw(receive_ms,processed_ms,payload) VALUES(?,?,?)", (received, processed, encode(payload)))
                        health["sports_updates"] += 1
                        if isinstance(payload, dict) and "gameId" in payload:
                            sports_state[str(payload["gameId"])] = (payload, received, processed)
                            await queue.put({"sports_update": True})
            except asyncio.CancelledError:
                raise
            except Exception as error:
                runner.issues.append("SPORTS_"+type(error).__name__)
                source.health(utc_ms(), "disconnected", "sports_ws_gap")
                await asyncio.sleep(1)

    tasks = [asyncio.create_task(runner.watchdog()), asyncio.create_task(metadata()), asyncio.create_task(books_loop()), asyncio.create_task(sports_loop()), asyncio.create_task(market_ws_loop())]
    runner.watchdog_task = tasks[0]
    started = utc_ms()
    try:
        while time.monotonic() < deadline:
            current = utc_ms()
            if market_state.get('end_info'):
                runner.observe_game_information(market_state['end_info'],window,current)
            if runner.bound_market:
                game = sports_state.get(str(runner.bound_market.get("polymarket_game_id")))
                if game:
                    info = information(game[0], runner.bound_market, game[1], game[2])
                    if runner.game_information != info:
                        runner.game_information = info
                        with source.db:
                            source.db.execute("INSERT INTO game_information(received_ms,processed_ms,payload) VALUES(?,?,?)", (game[1], game[2], encode(info)))
                    runner.observe_game_information(info, window, current)
            if window.stop_reason(current):
                stop_reason = window.stop_reason(current)
                break
            if control:
                await control.pump()
                if runner.stopping.is_set():
                    stop_reason = "OPERATOR_STOP_NOT_COMPLETE"
                    break
            if tasks[0].done():
                tasks[0].result()  # never continue risk submission if watchdog died
                raise RuntimeError("watchdog stopped")
            try:
                message = await asyncio.wait_for(queue.get(), min(.25, max(.01, deadline-time.monotonic())))
            except asyncio.TimeoutError:
                continue
            if isinstance(message, dict):
                if runner.latest and "trade" in message:
                    trade = {**message["trade"], "strategy_available_ms": max(utc_ms(), om.now)}
                    runner.feed(list(runner.latest.values()), [trade], provenance="REAL_PUBLIC_WS_TRADE; received/processed/exchange clocks retained", reuse=True, message_epoch=message.get("connection_epoch"))
                    continue
                if not runner.latest or not message.get("sports_update"):
                    continue
                rows = list(runner.latest.values())
                reuse = True
            else:
                rows = message
                reuse = False
            contract = (contracts or {}).get(str(market_id), {})
            if policy.research_phase and runner.bound_market and not contract:
                m=runner.bound_market
                contract={'event_id':m['event_id'],'game_id':str(m.get('polymarket_game_id') or ''),
                          'home_team':m.get('home_team'),'away_team':m.get('away_team'),
                          'identity_evidence':'revalidated Gamma explicit game ID and home/away roles'}
            item = sports_state.get(str(contract.get("game_id")))
            if item:
                for f in rows:
                    f["sports"] = normalize(item[0], item[1], contract)
                    f["sports"].update(processed_ms=item[2], strategy_available_ms=item[2])
            runner.feed(rows, provenance="REAL_PUBLIC_REST_AND_SPORTS; no synthetic stage", reuse=reuse)
    except BaseException as error:
        stop_reason = "SERVICE_ERROR_"+type(error).__name__+"_NOT_COMPLETE"
        runner.issues.append(stop_reason)
        source.health(utc_ms(), "disconnected", stop_reason)
        raise
    finally:
        if control:
            control.begin_stop()
        runner.stopping.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        om.kill("runner_window_ended")
        await asyncio.sleep(min(.2, policy.request_timeout_ms/1000+.02))
        om.advance(max(om.now, utc_ms()))
        om.reconcile()
        om.kill("runner_stopped_keep_halted")
        result = report(om)
        result["recording_coverage"] = window.coverage(utc_ms(), stop_reason)
        result["game_information"] = runner.game_information
        result["bound_market"] = runner.bound_market
        result["runner"] = {"start_ms": started, "stop_ms": utc_ms(), "duration_requested_s": seconds, "market_id": str(market_id), "event_slug": event_slug,
                            "public_input_batches": runner.events, "wall_watchdog_ticks": runner.ticks, "health": health,
                            "issues": sorted(set(runner.issues)), "source_provenance": "real public GET/WS", "phase_authority": "MANUAL_CONFIRMED" if manual_file else "AUTOMATIC_UNVERIFIED_UNTIL_CONTRACT_PROVEN",
                            "execution_evidence": result["execution_evidence"], "game_full_window_verified": False,
                            "reduce_s": policy.reduce_remaining_s, "close_s": policy.close_remaining_s, "parameters_calibrated": False,
                            "initial_inventory_source": "cash-only, no injected shares", "public_trades_consumed": health["public_trades"],
                            "fill_scope": "same asynchronous L0/L1/L2 proxies; zero prints is not proof of no real market trades"}
        from ..pilot import research_report
        result["pilot"] = research_report(result, source.db, om.db)
        if stop_reason == "OBSERVED_GAME_END_PLUS_TAIL":
            Path(str(db_path)+"-REAL_GAME_SESSION_REPORT.json").write_text(json.dumps(result["pilot"], ensure_ascii=False, indent=2), encoding="utf-8")
        from .__main__ import save_report
        save_report(om, result)
        if control:
            control.finished(result)
        source.health(utc_ms(), "stopped", "bounded_paper_runner")
        await api.close()
        source.close()
        store.close()
    return result
