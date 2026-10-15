"""Finite human phase authorization for controlled Paper, never auto evidence."""
from ..fair_value.providers import reject_secrets


def permission(grant, market, at, max_age_ms=15000):
    try:
        if not grant or grant.get("authority_mode") != "MANUAL_CONFIRMED":
            raise ValueError("manual_confirmation_missing")
        reject_secrets(grant)
        if grant["market_id"] != market["market_id"] or grant["event_id"] != market["event_id"] or not grant["game_id"]:
            raise ValueError("manual_game_identity_mismatch")
        if not all(grant.get(k) for k in ("confirmation_id", "operator_id", "evidence", "exit_instructions")):
            raise ValueError("manual_evidence_missing")
        confirmed, expiry = grant["confirmed_ms"], grant["expires_ms"]
        if type(confirmed) is not int or type(expiry) is not int or not confirmed <= at < expiry or not 0 < expiry-confirmed <= max_age_ms or grant.get("strategy_available_ms", confirmed) > at:
            raise ValueError("manual_confirmation_expired_or_future")
        chosen = grant["permission"]
        if chosen not in {"ACTIVE_MM", "REDUCE_ONLY", "MM_CLOSED", "PRE_GAME_OBSERVE", "UNKNOWN"}:
            raise ValueError("invalid_manual_permission")
        if market.get("event_ended") or market.get("status", {}).get("closed"):
            return "MM_CLOSED", []
        if chosen in {"MM_CLOSED", "PRE_GAME_OBSERVE", "UNKNOWN"}:
            return chosen, []
        if market.get("event_live") is not True or grant.get("status_observation") != "IN_PROGRESS" or grant.get("clock_observation") != "RUNNING":
            raise ValueError("manual_status_conflict_or_clock_stopped")
        period, remaining = grant["period"], grant["remaining_s"]
        if type(period) is not int or type(remaining) is not int or period < 1 or not 0 <= remaining <= (720 if period <= 4 else 300):
            raise ValueError("invalid_manual_clock")
        return chosen, []
    except (KeyError, ValueError, TypeError) as error:
        return "UNKNOWN", [str(error)]


def effective(grant, market, at, policy):
    state, reasons = permission(grant, market, at, policy.manual_max_age_ms)
    if state in {"ACTIVE_MM", "REDUCE_ONLY"}:
        period, remaining = grant["period"], grant["remaining_s"]
        if period >= 4 and remaining <= policy.close_remaining_s:
            return "MM_CLOSED", ["manual_late_close_threshold"]
        if period > 4 or (period == 4 and remaining <= policy.reduce_remaining_s):
            return "REDUCE_ONLY", ["manual_late_reduce_threshold"]
    return state, reasons
