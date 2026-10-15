import csv
import hashlib
import json
import math
import re
import time
from pathlib import Path
import httpx
from ..fair_value.maths import decimal
from ..storage import encode

GAMMA = "https://gamma-api.polymarket.com"
DATA = "https://data-api.polymarket.com"
ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")
NBA_SLUG = re.compile(r"nba-[a-z0-9]+-[a-z0-9]+-\d{4}-\d{2}-\d{2}")


class IncompleteWindow(ValueError):
    pass


def paginate(fetch, path, parameters, start, end, limit=500, cap=5000):
    """Inclusive integer-second windows. Same-second overflow is unresolvable.

    On overflow discard parent pages, bisect into [lo,mid]/[mid+1,hi].
    No timestamp-only cursor skips tied timestamps.
    """
    result = []
    offset = 0
    while offset <= cap:
        rows = fetch(path, {**parameters, "start": start, "end": end, "limit": limit, "offset": offset})
        if not isinstance(rows, list):
            raise IncompleteWindow("unexpected pagination schema")
        if any(type(row.get("timestamp")) is not int or not start <= row["timestamp"] <= end for row in rows):
            raise IncompleteWindow("server returned out-of-window/invalid timestamp")
        result.extend(rows)
        if len(rows) < limit:
            return result
        offset += limit
    if start >= end:
        raise IncompleteWindow("same-second pagination cap; truncated, no safe cursor")
    mid = (start+end)//2
    return paginate(fetch, path, parameters, start, mid, limit, cap)+paginate(fetch, path, parameters, mid+1, end, limit, cap)


def amount_stats(rows):
    amounts = sorted(decimal(r["amount"]) for r in rows)
    if not amounts:
        return {"sample_count": 0, "median_dollars": None, "p90_dollars": None,
                "le_5_count_fraction": None, "le_10_count_fraction": None,
                "le_5_amount_fraction": None, "le_10_amount_fraction": None}
    count, total = len(amounts), sum(amounts, decimal(0))
    median = amounts[count//2] if count % 2 else (amounts[count//2-1]+amounts[count//2])/2
    result = {"sample_count": count, "median_dollars": str(median), "p90_dollars": str(amounts[math.ceil(.9*count)-1]), "total_dollars": str(total), "quantile_definition": "median, nearest-rank P90"}
    for threshold in (5, 10):
        subset = [a for a in amounts if a <= threshold]
        result[f"le_{threshold}_count_fraction"] = str(decimal(len(subset))/count)
        result[f"le_{threshold}_amount_fraction"] = str(sum(subset, decimal(0))/total) if total else None
    return result


class EvidenceClient:
    def __init__(self, out, transport=None):
        self.out, self.pages = Path(out), []
        self.out.mkdir(parents=True, exist_ok=True)
        self.client = httpx.Client(timeout=15, follow_redirects=False, transport=transport)

    def get(self, host, path, params=None):
        if len(self.pages) >= 60:
            raise IncompleteWindow("lightweight research request budget exhausted; narrow the window")
        if (host, path) not in {(GAMMA, "/public-profile"), (GAMMA, "/sports"), (DATA, "/trades"), (DATA, "/activity")} and not (host == GAMMA and path.startswith("/events/slug/") and NBA_SLUG.fullmatch(path.removeprefix("/events/slug/"))):
            raise ValueError("public research route not allowlisted")
        request = {"url": host+path, "params": params or {}, "fetched_ms": int(time.time()*1000), "api_version": "Data API v1 explicit legacy contract" if host == DATA else "Gamma public", "page": len(self.pages)+1}
        raw = b""
        try:
            response = self.client.get(host+path, params=params)
            raw = response.content
            request["http_status"] = response.status_code
            response.raise_for_status()
            body = response.json()
        except Exception as error:
            request["error"] = type(error).__name__
            raise
        finally:
            digest = hashlib.sha256(raw).hexdigest()
            name = f"response-{request['page']:05d}-{digest[:12]}.bin"
            (self.out/name).write_bytes(raw)
            request.update(sha256=digest, raw_file=name)
            self.pages.append(request)
            (self.out/"requests.json").write_text(json.dumps(self.pages, indent=2), encoding="utf-8")
        return body

    def close(self):
        self.client.close()


def collect_wallet(address, out, start, end, transport=None):
    if not ADDRESS.fullmatch(address) or type(start) is not int or type(end) is not int or not 0 < start <= end <= int(time.time()):
        raise ValueError("valid public address and fixed past time window required")
    out = Path(out)
    if out.exists():
        raise ValueError("refuse to overwrite research evidence")
    client = EvidenceClient(out, transport)
    fields = ["raw_id", "event_id", "market_id", "condition_id", "asset_id", "timestamp", "season_kind", "market_type", "side", "price", "shares", "amount", "role", "role_evidence", "transaction_hash", "original_order_size"]
    summary = {"input_address": address, "window_start_s": start, "window_end_s": end, "status": "BLOCKED", "complete_window": False,
               "nba_statistics": None, "role_known_coverage": None, "known_maker_fraction": None,
               "inventory_reconstruction": "UNKNOWN: opening balances and share transfers unavailable", "round_trip_evidence": "UNKNOWN",
               "missing": ["maker/taker proof", "original order IDs and sizes", "historical books/queues", "opening inventory", "share transfer history"],
               "api_version": "v1 (legacy, explicitly verified docs); no v2 fields used", "not_copy_trading": True}
    fills, unmatched, rows = [], [], []
    try:
        profile = client.get(GAMMA, "/public-profile", {"address": address})
        proxy = profile.get("proxyWallet")
        if not isinstance(proxy, str) or not ADDRESS.fullmatch(proxy):
            raise IncompleteWindow("official profile did not prove proxy wallet")
        summary.update(proxy_wallet=proxy, identity_evidence={"source": "Gamma /public-profile", "input": address, "proxy": proxy, "name": profile.get("name"), "substitution_explicit": proxy.lower() != address.lower()})
        params = {"user": proxy, "sortBy": "TIMESTAMP", "sortDirection": "ASC", "excludeDepositsWithdrawals": "false"}
        rows = paginate(lambda p, q: client.get(DATA, p, q), "/activity", params, start, end)
        trades = paginate(lambda p, q: client.get(DATA, p, q), "/trades", {"user": proxy, "takerOnly": "false"}, start, end, limit=1000, cap=10000)
        # Do not infer maker from BUY/SELL or from incomplete all-vs-taker sets.
        if any(str(r.get("proxyWallet", "")).lower() != proxy.lower() for r in rows+trades):
            raise IncompleteWindow("returned wallet identity mismatch")
        sports = client.get(GAMMA, "/sports")
        nba = next((s for s in sports if s.get("sport") == "nba"), None)
        if not nba or not nba.get("series"):
            raise IncompleteWindow("NBA series identity unavailable")
        metadata, seen = {}, set()
        activity_counts, income = {}, []
        for raw in rows:
            key = hashlib.sha256(encode(raw).encode()).hexdigest()
            if key in seen:
                continue
            seen.add(key)
            kind = raw.get("type", "UNKNOWN")
            activity_counts[kind] = activity_counts.get(kind, 0)+1
            if kind in {"MAKER_REBATE", "REWARD"}:
                income.append({"raw_id": key, "type": kind, "amount": raw.get("usdcSize"), "market_id": None, "allocation": "unallocated; account level, no NBA attribution proof"})
            if kind != "TRADE":
                continue
            slug = raw.get("eventSlug", "")
            if not NBA_SLUG.fullmatch(slug):
                unmatched.append({"raw_id": key, "reason": "no_official_nba_event_mapping"})
                continue
            if slug not in metadata:
                metadata[slug] = client.get(GAMMA, "/events/slug/"+slug)
            event = metadata[slug]
            if not any(str(s.get("id")) == str(nba["series"]) for s in event.get("series", [])):
                unmatched.append({"raw_id": key, "reason": "not_official_nba_series"})
                continue
            market = next((m for m in event.get("markets", []) if m.get("conditionId") == raw.get("conditionId")), None)
            if not market:
                unmatched.append({"raw_id": key, "reason": "condition_not_in_event"})
                continue
            asset_ids = market.get("positionIds") if market.get("version") == "v2" else market.get("clobTokenIds")
            asset_ids = json.loads(asset_ids) if isinstance(asset_ids, str) else asset_ids
            if not asset_ids or str(raw.get("asset")) not in [str(a) for a in asset_ids]:
                unmatched.append({"raw_id": key, "reason": "asset_identity_mismatch"})
                continue
            text = (event.get("title", "")+" "+market.get("question", "")).lower()
            season = "PRESEASON" if "preseason" in text or "pre-season" in text else "REGULAR_SEASON" if "2026-10-20" <= slug[-10:] <= "2027-04-11" and not any(x in text for x in ("cup final", "all-star", "play-in")) else "OTHER_OR_UNKNOWN"
            mtype = market.get("sportsMarketType", "UNKNOWN")
            if mtype != "moneyline" or market.get("slug") != slug:
                unmatched.append({"raw_id": key, "reason": "nba_non_full_game_moneyline", "market_type": mtype, "season_kind": season})
                continue
            price, size = decimal(raw["price"]), decimal(raw["size"])
            if not 0 <= price <= 1 or size <= 0 or raw.get("side") not in {"BUY", "SELL"}:
                raise IncompleteWindow("invalid trade cash/size/side")
            cash = decimal(raw["usdcSize"]) if raw.get("usdcSize") is not None else price*size
            if cash < 0 or abs(cash-price*size) > decimal(".01"):
                raise IncompleteWindow("cash field disagrees with price times shares")
            fills.append(dict(zip(fields, [key, str(event["id"]), str(market["id"]), raw["conditionId"], raw["asset"], raw["timestamp"], season, mtype, raw["side"], str(price), str(size), str(cash), "UNKNOWN", "v1 public rows contain no verified execution role", raw.get("transactionHash"), "UNKNOWN"])))
        summary.update(status="COMPLETE_WINDOW", complete_window=True, nba_statistics=amount_stats(fills),
                       season_cohorts={season: amount_stats([r for r in fills if r["season_kind"] == season]) for season in {r["season_kind"] for r in fills}},
                       role_known_coverage="0" if fills else None, known_maker_fraction=None, nba_samples=len(fills),
                       unmatched=unmatched, activity_counts=activity_counts, income=income, all_trade_rows=len(trades),
                       duplicate_canonical_activity_rows=len(rows)-len(seen),
                       identity_limit="canonical-row observations deduped; identical true fills may be indistinguishable; transaction hash is not order ID",
                       inventory_window_delta="not computed without verified transfer/conversion semantics; full inventory UNKNOWN")
    except Exception as error:
        summary.update(status="TRUNCATED" if isinstance(error, IncompleteWindow) else "BLOCKED", error={"type": type(error).__name__, "reason": str(error) if isinstance(error, IncompleteWindow) else "public API request/schema failed; inspect raw evidence"}, complete_window=False, nba_statistics=None)
        fills = []
    finally:
        summary["http_pages"] = len(client.pages)
        (out/"wallet_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        with (out/"nba_fills.csv").open("w", encoding="utf-8", newline="") as fp:
            writer = csv.DictWriter(fp, fieldnames=fields)
            writer.writeheader()
            writer.writerows(fills)
        facts = ["# Public wallet evidence", "", f"Status: {summary['status']}", "", f"FACT: supplied address {address}; fixed window [{start}, {end}].", f"FACT: {len(client.pages)} request records with parameters, statuses, raw bytes and SHA256 in requests.json.",
                 "INFERENCE: canonical-equivalent activity observations treated as duplicates; this is not a proof of original order identity.",
                 "UNKNOWN: Maker/Taker role, original order size, opening inventory/share transfers, historical queue, cancel speed, historical markout and independent total PnL.",
                 "UNKNOWN: unallocated account rewards cannot be assigned to NBA.",
                 "FACT: no wallet/signing/order interface; failed API is BLOCKED, pagination failure is TRUNCATED; no statistics emitted for incomplete windows."]
        (out/"wallet_evidence.md").write_text("\n".join(facts)+"\n", encoding="utf-8")
        client.close()
    return summary
