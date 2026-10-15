import asyncio
import json
import re
from datetime import datetime, timezone
from collections import Counter
from decimal import Decimal, InvalidOperation
from .teams import team_code

import httpx

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
# NBA official 2026-27 calendar, Eastern dates. Cup final is not regular season.
SEASON_START = "2026-10-20"
SEASON_END = "2027-04-11"
CALENDAR_SOURCE = "https://pr.nba.com/2026-27-nba-regular-season-schedule"
TEAMS = set("atl bos bkn cha chi cle dal den det gsw hou ind lac lal mem mia mil min nop nyk okc orl phi phx por sac sas tor uta was".split())
EXCLUDED_DATES = {"2026-12-11", "2027-02-19", "2027-02-20", "2027-02-21"}


def utc_ms():
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def parse_time(value):
    d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if d.tzinfo is None:
        raise ValueError("timezone required")
    return int(d.timestamp() * 1000)


def array(value):
    return json.loads(value) if isinstance(value, str) else value


def select_market(event, market, series_id, now, ordering=None, scope="pregame"):
    """Fail closed on missing identity/type/time. Never infer time from listing date."""
    slug = event.get("slug", "")
    match = re.fullmatch(r"nba-([a-z0-9]+)-([a-z0-9]+)-(\d{4}-\d{2}-\d{2})", slug)
    if not match or not any(str(s.get("id")) == series_id for s in event.get("series", [])):
        return None, "not_nba_game"
    if match[1] not in TEAMS or match[2] not in TEAMS or match[1] == match[2]:
        return None, "not_nba_teams"
    date = match[3]
    text = (event.get("title", "") + " " + market.get("question", "") + " " + event.get("description", "")).lower()
    if not SEASON_START <= date <= SEASON_END or date in EXCLUDED_DATES or any(s in text for s in ("preseason", "pre-season", "summer league", "all-star", "cup final", "cup championship", "play-in")):
        return None, "not_regular_season"
    if market.get("sportsMarketType") != "moneyline" or market.get("slug") != slug:
        return None, "not_full_game_moneyline"
    try:
        start = market.get("gameStartTime") or event["startTime"]
        start_ms = parse_time(start)
        start_date = datetime.fromtimestamp(start_ms / 1000, timezone.utc).date().isoformat()
        if not SEASON_START <= start_date <= "2027-04-12":
            return None, "start_outside_season"
        version = market.get("version", "v1")
        if version not in ("v1", "v2"):
            return None, "unsupported_protocol"
        tokens = array(market.get("positionIds") if version == "v2" else market.get("clobTokenIds"))
        outcomes = array(market["outcomes"])
        if len(tokens) != 2 or len(outcomes) != 2 or len(set(tokens)) != 2:
            raise ValueError("binary identity")
        tick, minimum = Decimal(str(market["orderPriceMinTickSize"])), Decimal(str(market["orderMinSize"]))
        if not tick.is_finite() or not minimum.is_finite() or not 0 < tick <= 1 or minimum <= 0:
            raise ValueError("invalid market parameters")
        if not all(market.get(k) for k in ("id", "conditionId", "question")) or not event.get("id"):
            raise ValueError("missing identity")
    except (KeyError, ValueError, TypeError, InvalidOperation):
        return None, "missing_metadata"
    if scope not in {"pregame", "full-game"}:
        raise ValueError("explicit supported collection scope required")
    if scope == "pregame" and (start_ms <= now or event.get("live") or event.get("ended")):
        return None, "not_pregame"
    if scope == "pregame" and not (event.get("active") and market.get("active") and not event.get("closed") and not event.get("archived") and not market.get("closed") and not market.get("archived") and market.get("enableOrderBook")):
        return None, "inactive"
    roles = {}
    for role in ("home", "away"):
        teams = [t for t in event.get("teams", []) if isinstance(t, dict) and t.get("ordering") == role and t.get("league") == "nba"]
        if len(teams) == 1:
            abbreviation, name = team_code(teams[0].get("abbreviation")), team_code(teams[0].get("name"))
            if abbreviation and name and abbreviation != name:
                continue
            roles[role] = abbreviation or name
    if set(roles.values()) != {match[1], match[2]}:
        roles = {}
    result = {"venue": "polymarket.com", "season": "2026-27", "calendar_source": CALENDAR_SOURCE,
              "event_id": str(event["id"]), "market_id": str(market["id"]), "condition_id": market["conditionId"],
              "slug": slug, "question": market["question"], "game_start": start, "game_start_ms": start_ms,
              "version": market.get("version", "v1"), "outcomes": [{"label": label, "asset_id": str(token)} for label, token in zip(outcomes, tokens)],
              "description": market.get("description", ""), "resolution_source": market.get("resolutionSource"),
              "market_type": market.get("sportsMarketType"), "league": "nba", "team_ordering": ordering,
              "away_team": roles.get("away"), "home_team": roles.get("home"),
              "home_away_evidence": "event_teams" if len(roles) == 2 else None, "event_teams": event.get("teams", []),
              "event_live": event.get("live"), "event_ended": event.get("ended"),
              "yes_asset_id": next((str(t) for l, t in zip(outcomes, tokens) if l.lower() == "yes"), None),
              "no_asset_id": next((str(t) for l, t in zip(outcomes, tokens) if l.lower() == "no"), None),
              "tick_size": str(market.get("orderPriceMinTickSize")), "minimum_order_size": str(market.get("orderMinSize")),
              "status": {k: market.get(k) for k in ("active", "closed", "archived", "acceptingOrders", "enableOrderBook", "restricted")}}
    return result, "selected"


class PublicAPI:
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=20, follow_redirects=False)

    async def close(self):
        await self.client.aclose()

    async def get(self, host, path, params=None):
        event_detail = host == GAMMA and re.fullmatch(r"/events/slug/nba-[a-z0-9]+-[a-z0-9]+-\d{4}-\d{2}-\d{2}", path)
        if (host, path) not in {(GAMMA, "/sports"), (GAMMA, "/events"), (CLOB, "/book")} and not event_detail:
            raise ValueError("Only allowlisted public GET routes permitted")
        for attempt in range(4):
            try:
                r = await self.client.get(host + path, params=params)
                r.raise_for_status()
                return r.json()
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code not in (429, 500, 502, 503, 504):
                    raise
                if attempt == 3:
                    raise
                await asyncio.sleep(2 ** attempt)

    async def scan(self, scope="pregame"):
        if scope not in {"pregame", "full-game"}:
            raise ValueError("unsupported collection scope")
        sports = await self.get(GAMMA, "/sports")
        nba = next((x for x in sports if x.get("sport") == "nba"), None)
        if not nba or not nba.get("series"):
            raise ValueError("NBA series missing in official sports metadata")
        counts, selected, offset = Counter(), {}, 0
        now = utc_ms()
        while True:
            params = {"series_id": str(nba["series"]), "limit": 100, "offset": offset, "order": "id", "ascending": "true"}
            if scope == "pregame":
                params["closed"] = "false"
            events = await self.get(GAMMA, "/events", params)
            if not isinstance(events, list):
                raise ValueError("Unexpected Gamma events schema")
            for event in events:
                detail = None
                for market in event.get("markets", []):
                    item, reason = select_market(event, market, str(nba["series"]), now, nba.get("ordering"), scope)
                    if item and not item.get("home_away_evidence"):
                        if detail is None:
                            detail = await self.get(GAMMA, "/events/slug/" + event["slug"])
                            if not isinstance(detail, dict):
                                raise ValueError("Unexpected Gamma event detail schema")
                        current = next((m for m in detail.get("markets", []) if str(m.get("id")) == str(market["id"])), None)
                        if current:
                            item, reason = select_market(detail, current, str(nba["series"]), utc_ms(), nba.get("ordering"), scope)
                        else:
                            item, reason = None, "event_detail_missing_market"
                    counts[reason] += 1
                    if item:
                        selected[item["market_id"]] = item
            if len(events) < 100:
                break
            offset += 100
            if offset > 10000:
                raise ValueError("Scan incomplete: pagination safety limit")
        return {"venue": "polymarket.com", "scanned_at_ms": now, "series_id": str(nba["series"]), "reasons": dict(counts), "markets": sorted(selected.values(), key=lambda x: x["game_start_ms"]), "status": "ok" if selected else "no_matching_markets"}

    async def book(self, asset):
        return await self.get(CLOB, "/book", {"token_id": asset})
