"""Repeatable public GET-only pilot preflight, including explicit failure classes."""
import argparse
import asyncio
import hashlib
import json
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path
import httpx
from .api import PublicAPI, GAMMA, parse_time, utc_ms
from .book import Book


class EvidenceAPI(PublicAPI):
    def __init__(self):
        super().__init__()
        self.requests = []

    async def get(self, host, path, params=None):
        row = {'host': host, 'path': path, 'params': params, 'started_ms': utc_ms()}
        self.requests.append(row)
        try:
            value = await super().get(host, path, params)
            row.update(http_status=200, received_ms=utc_ms(), sha256=hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest())
            return value
        except httpx.HTTPStatusError as error:
            row.update(http_status=error.response.status_code, error=type(error).__name__)
            raise
        except Exception as error:
            row.update(http_status=None, error=type(error).__name__)
            raise


async def preflight(start, end, output):
    api = EvidenceAPI()
    result = {'verified_ms': utc_ms(), 'window_utc': [start, end], 'venue': 'polymarket.com',
              'code_version': subprocess.check_output(['git','rev-parse','HEAD']).decode().strip(),
              'working_tree_dirty': bool(subprocess.check_output(['git','status','--porcelain']).strip()),
              'http_requests': api.requests, 'candidates': [], 'primary_sample': None,
              'phase_authorization': 'NO_QUOTE; clock/upstream age/pause contract not verified',
              'full_game_coverage': 'NOT_COMPLETE', 'Live': 'NO_GO'}
    try:
        scan = await api.scan('full-game', start_ms=parse_time(start), end_ms=parse_time(end), season_type='PRESEASON')
        result['unclassified_markets'] = scan.get('unclassified_markets', [])
        for m in scan['markets']:
            books = []
            for o in m['outcomes']:
                raw = await api.book(o['asset_id'])
                received = utc_ms()
                if str(raw.get('asset_id')) != o['asset_id'] or raw.get('market') != m['condition_id']:
                    raise ValueError('book identity mismatch')
                b = Book(m, o['asset_id'])
                b.apply({**raw, 'event_type': 'rest_snapshot'}, received)
                books.append({'raw': raw, 'snapshot': b.snapshot(received, 5), 'received_ms': received})
            t = datetime.fromtimestamp(m['game_start_ms']/1000, timezone.utc)
            result['candidates'].append({'market': m, 'start_utc': t.isoformat(),
                                         'start_utc8': t.astimezone(timezone(timedelta(hours=8))).isoformat(), 'books': books})
        eligible = [r for r in result['candidates'] if r['market'].get('home_away_evidence') and r['market']['rules_verified']
                    and all(not b['snapshot']['flags'] and b['snapshot']['bid'] is not None and b['snapshot']['ask'] is not None for b in r['books'])]
        from decimal import Decimal
        eligible.sort(key=lambda r: (sum(Decimal(b['snapshot']['ask'])-Decimal(b['snapshot']['bid']) for b in r['books']), r['market']['market_id']))
        result['status'] = 'MARKET_FOUND' if eligible else 'IDENTITY_AMBIGUOUS' if result['candidates'] or result['unclassified_markets'] else 'NO_MATCHING_MARKET'
        if eligible:
            result['primary_sample'] = eligible[0]['market']['market_id']
        result['selection_reason'] = 'Require verified season/team/time/rules and valid dual books; rank eligible samples by observed combined spread, never team popularity. Complementary depth is not summed liquidity.'
    except (httpx.HTTPError, OSError) as error:
        result.update(status='ACCESS_FAILED', error=type(error).__name__)
    except (ValueError, KeyError, TypeError) as error:
        result.update(status='SCHEMA_UNSUPPORTED', error=str(error))
    finally:
        await api.close()
    result['nba_official_local_http'] = []
    async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
        for url in ('https://www.nba.com/game/0012600018', 'https://cdn.nba.com/static/json/liveData/scoreboard/todaysScoreboard_00.json'):
            try:
                response = await client.get(url)
                result['nba_official_local_http'].append({'url': url, 'status': response.status_code, 'received_ms': utc_ms()})
            except httpx.HTTPError as error:
                result['nba_official_local_http'].append({'url': url, 'status': None, 'error': type(error).__name__})
    Path(output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--start', required=True)
    parser.add_argument('--end', required=True)
    parser.add_argument('--out', default='PRESEASON_PREFLIGHT.json')
    args = parser.parse_args()
    value = asyncio.run(preflight(args.start, args.end, args.out))
    print(json.dumps({k: value.get(k) for k in ('status','primary_sample','error')}, ensure_ascii=False))
