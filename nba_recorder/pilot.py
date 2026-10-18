"""Bounded recording lifecycle; independent of Paper risk and game-clock gates."""
class PilotWindow:
    def __init__(self, started_ms, limit_s=14400, postgame_s=600):
        if type(limit_s) is not int or not 60 <= limit_s <= 28800:
            raise ValueError('recording limit must be 60..28800 seconds')
        if type(postgame_s) is not int or not 0 <= postgame_s <= 3600:
            raise ValueError('postgame recording must be 0..3600 seconds')
        self.started_ms, self.limit_s, self.postgame_s = started_ms, limit_s, postgame_s
        self.finished_ms = None

    def observe(self, info, now, max_age_ms=15000):
        # Informational finished flag starts a tail, never settles a market.
        if (info.get('display_eligible') and not info.get('conflict') and info.get('game_finished')
                and 0 <= now-info.get('received_ms', -1) <= max_age_ms):
            if self.finished_ms is None:
                self.finished_ms = now

    def stop_reason(self, now):
        if now-self.started_ms >= self.limit_s*1000:
            return 'HARD_LIMIT_NOT_COMPLETE'
        if self.finished_ms is not None and now-self.finished_ms >= self.postgame_s*1000:
            return 'OBSERVED_GAME_END_PLUS_TAIL'
        return None

    def coverage(self, now, reason):
        return {'status': 'NOT_COMPLETE', 'start_ms': self.started_ms, 'stop_ms': now,
                'stop_reason': reason, 'observed_game_finished_ms': self.finished_ms,
                'postgame_s': self.postgame_s, 'hard_limit_s': self.limit_s,
                'limitation': 'End observation is informational. Full coverage requires independent gap/phase audit; never inferred from scheduled tip or market settlement.'}


def research_report(result, source_db, paper_db):
    """Actual recorded sample counts. Never substitute unknown game metrics with 0."""
    import json
    from math import ceil
    market = result.get('bound_market') or {}
    # Include only actual source frames for availability; watchdog re-use isn't a sample.
    samples = [json.loads(r[0]) for r in paper_db.execute('SELECT payload FROM ip_inputs')]
    unique_books = {}
    for e in samples:
        if e.get('kind') == 'WATCHDOG':
            continue
        for f in e['frames']:
            unique_books.setdefault((f['asset_id'], f.get('book_ref'), f['book_received_ms']), f)
    samples = list(unique_books.values())
    from .inplay.reference import book_data_risk
    from .inplay.policy import BookPolicy
    policy = BookPolicy(**result['policy'])
    valid = sum(not book_data_risk(f, policy, f['time_ms']) for f in samples)
    phases = [json.loads(r[0])['state'] for r in paper_db.execute('SELECT payload FROM ip_phase_log')]
    lags = sorted(r[0] for r in source_db.execute('SELECT lag_ms FROM event_processing WHERE lag_ms>=0'))
    health = [dict(zip(('time_ms', 'kind', 'detail'), r)) for r in source_db.execute('SELECT local_timestamp,kind,detail FROM health ORDER BY id')]
    return {'mode': 'REAL_PUBLIC_INPUT_PAPER_ONLY', 'season_type': market.get('season_type'),
            'nba_game_id': market.get('nba_game_id'), 'market_id': market.get('market_id'),
            'coverage': result['recording_coverage'],
            'book_mode': 'REST periodic samples; market WS raw events/trades; no gap backfill or continuous-book claim',
            'asset_book_samples': len(samples), 'quality_valid_samples': valid,
            'book_sample_availability': valid/len(samples) if samples else None,
            'phase_observations': len(phases),
            'phase_authorization_availability': sum(p in {'ACTIVE_MM', 'REDUCE_ONLY'} for p in phases)/len(phases) if phases else None,
            'availability_unit': 'unique asset/book_ref/receive samples; reuse/watchdog excluded, not wall-time/full-game coverage',
            'code_version': __import__('subprocess').check_output(['git','rev-parse','HEAD']).decode().strip(),
            'code_dirty_at_report': bool(__import__('subprocess').check_output(['git','status','--porcelain']).strip()),
            'source_event_counts': {r[0]: r[1] for r in source_db.execute('SELECT event_type,count(*) FROM raw_events GROUP BY event_type')},
            'sports_raw_updates_all_games': source_db.execute('SELECT count(*) FROM sports_raw').fetchone()[0],
            'matched_game_information_updates': source_db.execute('SELECT count(*) FROM game_information').fetchone()[0],
            'health_events_and_gaps': health, 'issues': result['runner']['issues'],
            'processing_lag_ms': {'samples': len(lags), **{f'P{n}': lags[max(0, ceil(len(lags)*n/100)-1)] if lags else None for n in (50,95,99)}},
            'execution_evidence': result['execution_evidence'], 'residual_inventory': result['residual_inventory'],
            'pnl': result['pnl_layers'], 'income_stages': result['income_stages'],
            'real_submission_calls': 0, 'Public_Observation': 'BOUNDED_SAMPLES_ONLY' if samples else 'NO_GO',
            'Controlled_Paper': 'NOT_VERIFIED', 'Full_game_Coverage': 'NO_GO', 'Live': 'NO_GO',
            'limitations': ['Complementary asset events are not independent liquidity or summed trade volume',
                            'Reconnect snapshots never reconstruct missing trades', 'No actual execution latency or profit is measured']}
