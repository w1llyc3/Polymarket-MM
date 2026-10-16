"""Information-only game adapter. Display evidence never grants phase permission."""
from .teams import team_code

# Individually checked official schedule evidence, not a date-based season guess.
# Direct local NBA HTTP was 403; official pages were independently read on 2026-10-16.
VERIFIED_GAMES = ({
    "nba_game_id": "0012600018", "away_team": "bos", "home_team": "phi",
    "start_ms": 1792191600000, "season_type": "PRESEASON",
    "source": "https://www.nba.com/game/0012600018",
    "classification_source": "https://www.nba.com/celtics/news/celtics-announce-2026-27-preseason-schedule",
    "verified_date": "2026-10-16", "method": "official pages cross-checked; not a sports phase contract",
},)


def schedule_match(away, home, start):
    return next((g.copy() for g in VERIFIED_GAMES if (away, home, start) ==
                 (g['away_team'], g['home_team'], g['start_ms'])), None)


def information(raw, market, received, processed):
    """Requires exact exchange game ID + team roles. No slug/date inference."""
    identity = (str(raw.get('gameId')) == str(market.get('polymarket_game_id'))
                and market.get('polymarket_game_id') is not None
                and raw.get('leagueAbbreviation') == 'NBA'
                and team_code(raw.get('homeTeam')) == market.get('home_team')
                and team_code(raw.get('awayTeam')) == market.get('away_team')
                and market.get('home_team') is not None and market.get('away_team') is not None)
    return {'source': 'wss://sports-api.polymarket.com/ws', 'display_eligible': identity,
            'identity_evidence': 'Gamma gameId + exact NBA team roles' if identity else None,
            'game_id': raw.get('gameId'), 'nba_game_id': market.get('nba_game_id'),
            'score': raw.get('score') if identity else None,
            'period': raw.get('period') if identity else None,
            'clock_raw': raw.get('elapsed') if identity else None, 'clock_semantics': 'UNVERIFIED',
            'status': raw.get('status') if identity else None,
            'game_phase': ('ENDED' if raw.get('ended') is True and raw.get('status') in {'Final','F/OT'} else 'BREAK' if raw.get('period') == 'HT' or raw.get('status') == 'Break' else 'LIVE' if raw.get('status') == 'InProgress' and raw.get('live') is True else 'SCHEDULED' if raw.get('status') == 'Scheduled' else 'UNKNOWN') if identity else 'UNKNOWN',
            'game_finished': raw.get('ended') is True and raw.get('status') in {'Final', 'F/OT'} if identity else False,
            'upstream_timestamp_ms': None, 'received_ms': received, 'processed_ms': processed,
            'strategy_available_ms': processed, 'paused': None, 'quote_authorized': False,
            'conflict': not identity, 'raw': raw,
            'limitations': ['No upstream age proof', 'No verified clock semantics or pause field',
                            'Receive age is not score age; heartbeat does not refresh a game']}

