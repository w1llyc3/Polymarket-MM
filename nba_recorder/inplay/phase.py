"""Persistent basketball phase gate. Clock semantics must be proven by caller.

No wall-clock extrapolation, score prediction, or automatic reopening of CLOSED.
"""
import json
from ..storage import encode

STATES = {"PRE_GAME_OBSERVE", "SYNCING", "ACTIVE_MM", "REDUCE_ONLY", "MM_CLOSED", "UNKNOWN"}


def install(db):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS ip_phase(market_id TEXT PRIMARY KEY,payload TEXT);
    CREATE TABLE IF NOT EXISTS ip_phase_log(id INTEGER PRIMARY KEY,market_id TEXT,time_ms INTEGER,payload TEXT);
    CREATE TRIGGER IF NOT EXISTS ip_phase_log_immutable_u BEFORE UPDATE ON ip_phase_log BEGIN SELECT RAISE(ABORT,'append-only'); END;
    CREATE TRIGGER IF NOT EXISTS ip_phase_log_immutable_d BEFORE DELETE ON ip_phase_log BEGIN SELECT RAISE(ABORT,'append-only'); END;
    """)


class PhaseController:
    def __init__(self, db, policy):
        self.db, self.policy = db, policy

    def get(self, market_id):
        row = self.db.execute("SELECT payload FROM ip_phase WHERE market_id=?", (market_id,)).fetchone()
        return json.loads(row[0]) if row else {"state": "UNKNOWN", "synced": 0, "latched_closed": False, "correction": False}

    def update(self, frame, at):
        old = self.get(frame["market_id"])
        state = {**old, "observed_ms": at, "reasons": []}
        sports = frame.get("sports", {})
        try:
            if old["latched_closed"]:
                state.update(state="MM_CLOSED", reasons=["closed_latch"])
            elif sports.get("ended") is True and sports.get("event_id") == frame["market"]["event_id"]:
                state.update(state="MM_CLOSED", latched_closed=True, reasons=["game_ended"])
            else:
                if sports.get("event_id") != frame["market"]["event_id"] or not sports.get("game_id") or not sports.get("identity_evidence"):
                    raise ValueError("unverified_game_identity")
                if not sports.get("source") or sports.get("conflict") or frame["market"].get("event_ended"):
                    raise ValueError("sports_source_conflict")
                for key in ("received_ms", "observed_ms"):
                    if not 0 <= at-sports[key] <= self.policy.sports_max_age_ms:
                        raise ValueError("sports_stale_or_future")
                if sports.get("status") == "Scheduled" and sports.get("live") is False:
                    state.update(state="PRE_GAME_OBSERVE", synced=0)
                else:
                    if sports.get("status") != "InProgress" or sports.get("live") is not True or sports.get("paused") is not False:
                        raise ValueError("paused_delayed_or_unknown_status")
                    if sports.get("clock_semantics") != "REMAINING_IN_PERIOD" or not sports.get("clock_evidence"):
                        raise ValueError("unknown_clock_semantics")
                    period, remaining = sports["period"], sports["remaining_s"]
                    if type(period) is not int or type(remaining) is not int or period < 1 or not 0 <= remaining <= (720 if period <= 4 else 300):
                        raise ValueError("invalid_period_clock")
                    if frame["market"].get("event_live") is not True:
                        raise ValueError("market_sports_live_conflict")
                    if old.get("period") and (period < old["period"] or (period == old["period"] and remaining > old["remaining_s"])):
                        state["correction"] = True
                    if state.get("correction"):
                        raise ValueError("clock_correction_manual_review")
                    if old.get("game_id") and old["game_id"] != sports["game_id"]:
                        raise ValueError("game_identity_changed")
                    repeat = sports["received_ms"] <= old.get("source_received_ms", -1)
                    count = old.get("synced", 0)+(not repeat)
                    state.update(period=period, remaining_s=remaining, game_id=sports["game_id"], source_received_ms=sports["received_ms"], synced=count)
                    if period >= 4 and remaining <= self.policy.close_remaining_s:
                        state.update(state="MM_CLOSED", latched_closed=True, reasons=["late_close_threshold"])
                    elif old.get("state") == "REDUCE_ONLY" or period > 4 or (period == 4 and remaining <= self.policy.reduce_remaining_s):
                        state.update(state="REDUCE_ONLY", reasons=["late_reduce_threshold"])
                    else:
                        state["state"] = "ACTIVE_MM" if count >= self.policy.sync_observations else "SYNCING"
        except (KeyError, TypeError, ValueError) as error:
            state.update(state="UNKNOWN", synced=0, reasons=[str(error)])
        state["evidence"] = sports
        with self.db:
            self.db.execute("INSERT INTO ip_phase VALUES(?,?) ON CONFLICT(market_id) DO UPDATE SET payload=excluded.payload", (frame["market_id"], encode(state)))
            self.db.execute("INSERT INTO ip_phase_log(market_id,time_ms,payload) VALUES(?,?,?)", (frame["market_id"], at, encode(state)))
        return state

    def boundary(self, frame, at):
        phase = self.get(frame["market_id"])
        if phase["latched_closed"]:
            return "MM_CLOSED"
        # Re-evaluate source freshness and actual game clock at submission,
        # never derive remaining time by subtracting wall-clock elapsed time.
        for key in ("received_ms", "observed_ms"):
            if not 0 <= at-frame.get("sports", {}).get(key, -10**15) <= self.policy.sports_max_age_ms:
                return "UNKNOWN"
        return phase["state"]
