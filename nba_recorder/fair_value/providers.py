"""Authorized read-only odds inputs. API secrets never enter archived payloads."""
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx

from ..api import parse_time, utc_ms
from ..teams import team_code


class ProviderError(ValueError):
    pass


@dataclass
class OddsBatch:
    provider: str
    received_ms: int
    raw: object
    quotes: list
    data_kind: str


class OddsProvider(Protocol):
    async def fetch(self) -> OddsBatch: ...


def reject_secrets(value):
    if isinstance(value, dict):
        for key, child in value.items():
            name = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if any(word in name for word in {"apikey", "privatekey", "apisecret", "authorization", "mnemonic", "seedphrase", "password", "signingkey", "accesstoken"}):
                raise ProviderError("Input contains credential fields; payload not archived")
            reject_secrets(child)
    elif isinstance(value, list):
        for child in value:
            reject_secrets(child)


class JSONProvider:
    def __init__(self, path, name="local-json"):
        self.path, self.name = Path(path), name

    async def fetch(self):
        received = utc_ms()
        raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        reject_secrets(raw)
        if raw.get("schema_version") != 1 or raw.get("data_kind") not in {"recorded", "synthetic"} or not isinstance(raw.get("quotes"), list):
            raise ProviderError("JSON requires schema_version=1, data_kind, quotes[]")
        if any(not isinstance(q, dict) for q in raw["quotes"]):
            raise ProviderError("Quote must be an object")
        # File timestamps cannot backdate when this collector actually learned a quote.
        return OddsBatch(self.name, received, raw, raw["quotes"], raw["data_kind"])


def odds_api_quotes(raw, odds_format, received, rule_contracts):
    if not isinstance(raw, list):
        raise ProviderError("Unexpected odds API response schema")
    quotes = []
    for event in raw:
        if not isinstance(event, dict):
            raise ProviderError("Unexpected event schema")
        try:
            start = parse_time(event["commence_time"])
            year = int(event["commence_time"][:4])
            if int(event["commence_time"][5:7]) < 10:
                year -= 1
            season = f"{year}-{str(year + 1)[-2:]}"
        except (ValueError, KeyError, TypeError):
            start, season = None, None
        for book in event.get("bookmakers", []):
            for market in book.get("markets", []):
                if market.get("key") != "h2h":
                    continue
                outcomes = market.get("outcomes", [])
                by_team = {team_code(o.get("name")): o.get("price") for o in outcomes}
                contract = rule_contracts.get(book.get("key"), {})
                timestamp = market.get("last_update") or book.get("last_update")
                try:
                    updated = parse_time(timestamp)
                except (ValueError, TypeError, AttributeError):
                    updated = None
                quotes.append({"source_id": "sportsbook:" + str(book.get("key")), "bookmaker": book.get("key"),
                               "event_id": event.get("id"), "league": "nba" if event.get("sport_key") == "basketball_nba" else event.get("sport_key"),
                               "season": season, "home_team": event.get("home_team"), "away_team": event.get("away_team"),
                               "start_ms": start, "updated_ms": updated, "status": "pregame" if start and start > received else "live_or_started",
                               "status_evidence": "scheduled_time_only", "odds_format": odds_format,
                               "home_odds": by_team.get(team_code(event.get("home_team"))), "away_odds": by_team.get(team_code(event.get("away_team"))),
                               "outcome_count": len(outcomes), "rules": contract.get("rules", {}), "rule_evidence": contract.get("rule_evidence", {}),
                               "raw_event": event, "raw_bookmaker": book})
    return quotes


class TheOddsAPIProvider:
    URL = "https://api.the-odds-api.com/v4/sports/basketball_nba/odds/"

    def __init__(self, regions="us", odds_format="decimal", contracts=None, transport=None):
        if odds_format not in {"american", "decimal"} or not regions or any(not r.isalpha() for r in regions.split(",")):
            raise ProviderError("Invalid regions/odds format")
        self.regions, self.odds_format = regions, odds_format
        self.contracts, self.transport = contracts or {}, transport
        reject_secrets(self.contracts)

    async def fetch(self):
        key = os.getenv("ODDS_API_KEY")
        if not key:
            raise ProviderError("ODDS_API_KEY is not configured; no request made")
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=False, transport=self.transport) as client:
                response = await client.get(self.URL, params={"apiKey": key, "regions": self.regions,
                    "markets": "h2h", "oddsFormat": self.odds_format, "dateFormat": "iso"})
                # Never propagate an exception containing the request URL/API key.
                if response.status_code != 200:
                    raise ProviderError(f"Odds API HTTP {response.status_code}; payload not archived")
                raw = response.json()
        except httpx.HTTPError:
            raise ProviderError("Odds API transport failure; request details omitted") from None
        except (json.JSONDecodeError, UnicodeError):
            raise ProviderError("Odds API invalid JSON; payload not archived") from None
        received = utc_ms()
        reject_secrets(raw)
        return OddsBatch("the-odds-api", received, raw,
                         odds_api_quotes(raw, self.odds_format, received, self.contracts), "live_api")
