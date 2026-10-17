"""Bounded research authority; never represents a verified live sports clock."""


def permission(frame, policy, at):
    market, sports = frame['market'], frame.get('sports') or {}
    if market.get('event_ended') or sports.get('ended'):
        return 'MM_CLOSED', []
    grant = frame.get('research_phase_grant')
    if grant:
        if grant.get('market_id') != market['market_id'] or not 0 <= at-grant.get('confirmed_ms', at+1) or at >= grant.get('expires_ms', 0):
            return 'UNKNOWN', ['manual_research_grant_expired_or_mismatch']
        return grant['state'], []
    if sports.get('event_id') != market['event_id'] or not sports.get('identity_evidence') or sports.get('conflict'):
        return 'UNKNOWN', ['research_identity_or_phase_conflict']
    stamp = sports.get('received_ms')
    if type(stamp) is not int or not 0 <= at-stamp <= policy.research_phase_age_ms:
        return 'UNKNOWN', ['research_phase_stale']
    if sports.get('paused') is True:
        return 'UNKNOWN', ['observed_pause']
    if sports.get('status') != 'InProgress' or sports.get('live') is not True or type(sports.get('period')) is not int:
        return 'UNKNOWN', ['research_start_not_confirmed']
    # No extrapolated countdown: unknown remaining time never becomes zero.
    period, remaining = sports['period'], sports.get('remaining_s')
    if period >= 4 and type(remaining) is int and sports.get('clock_evidence'):
        if remaining <= policy.close_remaining_s:
            return 'MM_CLOSED', []
        if remaining <= policy.reduce_remaining_s:
            return 'REDUCE_ONLY', []
    return ('REDUCE_ONLY' if period > 4 else 'ACTIVE_MM'), []
