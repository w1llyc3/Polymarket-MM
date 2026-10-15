"""Raw official sports stream recorder. elapsed is NEVER assumed remaining."""
import asyncio
import json
import sqlite3
import time
from pathlib import Path
from websockets.asyncio.client import connect
from ..api import utc_ms

SPORTS_WS = "wss://sports-api.polymarket.com/ws"


def normalize(raw, received, contract=None):
    """Contract describes independently verified field semantics, never guesses.

    The public stream's elapsed alone does not prove remaining time, suspension,
    or upstream age. Default output cannot authorize quoting.
    """
    import re
    contract = contract or {}
    result = {"source": SPORTS_WS, "received_ms": received, "observed_ms": None,
              "event_id": contract.get("event_id"), "game_id": str(raw.get("gameId", "")),
              "identity_evidence": None, "clock_semantics": "UNVERIFIED", "clock_evidence": None,
              "status": raw.get("status"), "live": raw.get("live"), "ended": raw.get("ended"), "paused": None,
              "period": None, "remaining_s": None, "raw": raw}
    if str(raw.get("gameId")) != str(contract.get("game_id")) or raw.get("leagueAbbreviation") != "NBA" or not contract.get("identity_evidence"):
        return result
    from ..teams import team_code
    if not contract.get("home_team") or not contract.get("away_team") or team_code(raw.get("homeTeam")) != contract["home_team"] or team_code(raw.get("awayTeam")) != contract["away_team"]:
        return result
    result["identity_evidence"] = contract["identity_evidence"]
    period = str(raw.get("period", "")).upper()
    quarter = re.fullmatch(r"(?:Q([1-4])|([1-4])Q)", period)
    overtime = re.fullmatch(r"(?:OT(\d*)|(\d+)OT)", period)
    result["period"] = int(quarter[1] or quarter[2]) if quarter else 4+int((overtime[1] or overtime[2] or "1")) if overtime else None
    clock = re.fullmatch(r"(\d{1,2}):([0-5]\d)", str(raw.get("elapsed", "")))
    if clock and contract.get("clock_semantics") == "REMAINING_IN_PERIOD" and contract.get("clock_evidence"):
        result.update(clock_semantics="REMAINING_IN_PERIOD", clock_evidence=contract["clock_evidence"], remaining_s=int(clock[1])*60+int(clock[2]))
    stamp = raw.get(contract.get("source_timestamp_ms_field", ""))
    if type(stamp) is int and contract.get("timestamp_evidence") and 0 <= stamp <= received:
        result["observed_ms"] = stamp
    paused = raw.get(contract.get("pause_field", ""))
    if type(paused) is bool and contract.get("pause_evidence"):
        result["paused"] = paused
    return result


async def record(path, seconds):
    if seconds <= 0:
        raise ValueError("positive bounded recording duration required")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript("""
    CREATE TABLE IF NOT EXISTS sports_raw(id INTEGER PRIMARY KEY,receive_ms INTEGER,processed_ms INTEGER,payload TEXT);
    CREATE TABLE IF NOT EXISTS sports_health(id INTEGER PRIMARY KEY,time_ms INTEGER,kind TEXT,detail TEXT);
    CREATE TRIGGER IF NOT EXISTS sports_raw_u BEFORE UPDATE ON sports_raw BEGIN SELECT RAISE(ABORT,'raw immutable'); END;
    CREATE TRIGGER IF NOT EXISTS sports_raw_d BEFORE DELETE ON sports_raw BEGIN SELECT RAISE(ABORT,'raw immutable'); END;
    """)
    deadline = time.monotonic()+seconds
    updates, connections = 0, 0
    try:
        while time.monotonic() < deadline:
            try:
                async with connect(SPORTS_WS, ping_interval=None, open_timeout=10, close_timeout=2) as ws:
                    connections += 1
                    with db:
                        db.execute("INSERT INTO sports_health(time_ms,kind,detail) VALUES(?,'connected',?)", (utc_ms(), SPORTS_WS))
                    while time.monotonic() < deadline:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), min(1, max(.01, deadline-time.monotonic())))
                        except asyncio.TimeoutError:
                            continue
                        received = utc_ms()
                        if raw == "ping":
                            await ws.send("pong")
                            continue
                        # Keep malformed/schema/new-league frames as raw evidence;
                        # no clock or identity authorizations inferred here.
                        with db:
                            db.execute("INSERT INTO sports_raw(receive_ms,processed_ms,payload) VALUES(?,?,?)", (received, utc_ms(), raw.decode() if isinstance(raw, bytes) else raw))
                        updates += 1
            except Exception as error:
                with db:
                    db.execute("INSERT INTO sports_health(time_ms,kind,detail) VALUES(?,'disconnected',?)", (utc_ms(), type(error).__name__))
                await asyncio.sleep(min(1, max(0, deadline-time.monotonic())))
        return {"connections": connections, "raw_updates": updates, "quote_permission": "UNKNOWN", "clock_semantics": "UNVERIFIED", "real_submission_calls": 0}
    finally:
        with db:
            db.execute("INSERT INTO sports_health(time_ms,kind,detail) VALUES(?,'stopped','record-only')", (utc_ms(),))
        db.close()
