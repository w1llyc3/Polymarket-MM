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
    CREATE TABLE IF NOT EXISTS ip_manual_confirmations(id TEXT PRIMARY KEY,available_ms INTEGER,payload TEXT);
    CREATE TRIGGER IF NOT EXISTS ip_manual_u BEFORE UPDATE ON ip_manual_confirmations BEGIN SELECT RAISE(ABORT,'append-only'); END;
    CREATE TRIGGER IF NOT EXISTS ip_manual_d BEFORE DELETE ON ip_manual_confirmations BEGIN SELECT RAISE(ABORT,'append-only'); END;
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
        if self.policy.research_phase:
            from .research_phase import permission
            chosen, errors = permission(frame, self.policy, at)
            sport_clock=(sports.get('period'),sports.get('remaining_s'))
            previous_clock=old.get('research_clock')
            if not frame.get('research_phase_grant') and chosen in {'ACTIVE_MM','REDUCE_ONLY'} and sports.get('clock_evidence') and all(type(v) is int for v in sport_clock):
                if previous_clock and (sport_clock[0]<previous_clock[0] or (sport_clock[0]==previous_clock[0] and sport_clock[1]>previous_clock[1])):
                    chosen, errors='UNKNOWN',['clock_correction_review_new_observation_required']
                    state['review_received_ms']=sports.get('received_ms')
                elif old.get('review_received_ms') is not None and sports.get('received_ms',0)<=old['review_received_ms']:
                    chosen,errors='UNKNOWN',['clock_correction_wait_new_observation']
                else:state.pop('review_received_ms',None)
                state['research_clock']=sport_clock
            if old['latched_closed']:
                chosen = 'MM_CLOSED'
            elif old['state'] == 'REDUCE_ONLY' and chosen == 'ACTIVE_MM':
                chosen = 'REDUCE_ONLY'
            state.update(state=chosen, reasons=errors, latched_closed=old['latched_closed'] or chosen=='MM_CLOSED',
                         phase_authority='MANUAL_RESEARCH' if frame.get('research_phase_grant') else 'AUTOMATIC_RESEARCH',
                         period=(frame.get('research_phase_grant') or sports).get('period'),
                         evidence=frame.get('research_phase_grant') or sports, exact_clock_verified=False)
            with self.db:
                self.db.execute('INSERT INTO ip_phase VALUES(?,?) ON CONFLICT(market_id) DO UPDATE SET payload=excluded.payload', (frame['market_id'], encode(state)))
                if {k:v for k,v in state.items() if k!='observed_ms'} != {k:v for k,v in old.items() if k!='observed_ms'}:
                    self.db.execute('INSERT INTO ip_phase_log(market_id,time_ms,payload) VALUES(?,?,?)',(frame['market_id'],at,encode(state)))
            return state
        if self.policy.allow_manual_phase:
            from .manual import effective
            grant = frame.get("manual_confirmation")
            chosen, reasons = effective(grant, frame["market"], at, self.policy)
            if old["latched_closed"]:
                chosen, reasons = "MM_CLOSED", ["closed_latch"]
            elif old["state"] == "REDUCE_ONLY" and chosen == "ACTIVE_MM":
                chosen = "REDUCE_ONLY"
            if chosen in {"ACTIVE_MM", "REDUCE_ONLY"} and old.get("period") and (grant["period"] < old["period"] or (grant["period"] == old["period"] and grant["remaining_s"] > old["remaining_s"])):
                chosen, reasons = "UNKNOWN", ["manual_clock_correction_requires_review"]
            state.update(state=chosen, reasons=reasons, phase_authority="MANUAL_CONFIRMED", not_unattended=True,
                         latched_closed=old["latched_closed"] or chosen == "MM_CLOSED", evidence=grant)
            if grant and chosen in {"ACTIVE_MM", "REDUCE_ONLY"}:
                state.update(period=grant["period"], remaining_s=grant["remaining_s"])
            with self.db:
                self.db.execute("INSERT INTO ip_phase VALUES(?,?) ON CONFLICT(market_id) DO UPDATE SET payload=excluded.payload", (frame["market_id"], encode(state)))
                self.db.execute("INSERT INTO ip_phase_log(market_id,time_ms,payload) VALUES(?,?,?)", (frame["market_id"], at, encode(state)))
            return state
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
        if self.policy.research_phase:
            from .research_phase import permission
            if phase['state'] not in {'ACTIVE_MM','REDUCE_ONLY'}:
                return phase['state']
            chosen, _ = permission(frame, self.policy, at)
            return 'REDUCE_ONLY' if phase['state']=='REDUCE_ONLY' and chosen=='ACTIVE_MM' else chosen
        if self.policy.allow_manual_phase:
            from .manual import effective
            chosen, _ = effective(frame.get("manual_confirmation"), frame["market"], at, self.policy)
            return "REDUCE_ONLY" if phase["state"] == "REDUCE_ONLY" and chosen == "ACTIVE_MM" else chosen
        # Re-evaluate source freshness and actual game clock at submission,
        # never derive remaining time by subtracting wall-clock elapsed time.
        for key in ("received_ms", "observed_ms"):
            stamp = (frame.get("sports") or {}).get(key)
            if type(stamp) is not int or not 0 <= at-stamp <= self.policy.sports_max_age_ms:
                return "UNKNOWN"
        return phase["state"]
