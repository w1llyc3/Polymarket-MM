import re
from ..teams import team_code

RULE_KEYS = ("period", "overtime", "outcomes", "cancellation", "postponement")


def polymarket_rules(market):
    text = " ".join(market.get("description", "").lower().split())
    if market.get("market_type") != "moneyline" or market.get("league") != "nba":
        return None
    if any(phrase in text for phrase in ("not including", "excluding overtime", "excludes overtime", "overtime is excluded", "overtime not included", "will not resolve", "will not remain open")):
        return None
    if not re.search(r"including (?:any )?overtime", text):
        return None
    if not (re.search(r"if (?:the game is )?canceled entirely\b.*?\bresolves?(?: to)? 50-50\b", text)
            and re.search(r"if (?:the game is )?postponed\b.*?\bremain open until\b.*?\bcompleted\b", text)):
        return None
    if "regulation only" in text or "first half" in text:
        return None
    return {"period": "full_game", "overtime": True, "outcomes": 2,
            "cancellation": "half_payout", "postponement": "wait_until_completed"}


def match_event(market, quote, at_ms, tolerance_ms=0):
    reasons = []
    home, away = team_code(market.get("home_team")), team_code(market.get("away_team"))
    if not home or not away or home == away or market.get("home_away_evidence") not in {"event_teams", "synthetic_fixture"} or (market.get("home_away_evidence") == "synthetic_fixture" and not market.get("synthetic")):
        reasons.append("unverified_polymarket_home_away")
    if quote.get("league") != "nba" or quote.get("season") != market.get("season"):
        reasons.append("league_or_season_mismatch")
    if team_code(quote.get("home_team")) != home or team_code(quote.get("away_team")) != away:
        reasons.append("team_or_home_away_mismatch")
    try:
        start = int(quote["start_ms"])
        if abs(start - int(market["game_start_ms"])) > tolerance_ms:
            reasons.append("start_time_mismatch")
        if start <= at_ms or market["game_start_ms"] <= at_ms or quote.get("status") != "pregame":
            reasons.append("not_pregame_or_cancelled")
    except (KeyError, ValueError, TypeError):
        reasons.append("missing_start_time")
    status = market.get("status", {})
    if (market.get("venue") != "polymarket.com" or not status.get("active") or status.get("closed")
            or status.get("archived") or not status.get("enableOrderBook") or not status.get("acceptingOrders")
            or status.get("tracking_state") or market.get("event_live") or market.get("event_ended")):
        reasons.append("market_not_eligible")
    expected = polymarket_rules(market)
    rules = quote.get("rules", {})
    if expected is None or any(rules.get(k) != expected[k] or type(rules.get(k)) != type(expected[k]) for k in RULE_KEYS):
        reasons.append("unknown_or_mismatched_settlement_rules")
    evidence = quote.get("rule_evidence", {})
    try:
        if not str(evidence["source_url"]).startswith("https://") or not 0 <= int(evidence["verified_ms"]) <= at_ms:
            raise ValueError()
    except (KeyError, ValueError, TypeError):
        reasons.append("missing_rule_evidence")
    labels = [team_code(o.get("label")) for o in market.get("outcomes", [])]
    if len(labels) != 2 or set(labels) != {home, away} or None in labels:
        reasons.append("unverified_asset_team_mapping")
    return sorted(set(reasons))
