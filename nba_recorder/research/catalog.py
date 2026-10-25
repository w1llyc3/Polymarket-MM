import json
from pathlib import Path
from ..fair_value.store import read_connection, digest


def inspect(root, path):
    root, path = Path(root).resolve(), Path(path).resolve()
    relative = path.relative_to(root/'data').as_posix()
    db = read_connection(path)
    try:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
        if 'ip_inputs' not in tables:
            return None
        n, start, end = db.execute('SELECT count(*),min(time_ms),max(time_ms) FROM ip_inputs').fetchone()
        if not n:
            return None
        config = dict(db.execute('SELECT id,payload FROM ip_session_config'))
        origin = json.loads(config.get('console_origin', '{}'))
        binding = json.loads(config.get('runner_game', '{}'))
        source = origin.get('data_source') or ('REAL_OBSERVED' if binding else 'UNKNOWN')
        first_row=db.execute("SELECT payload FROM ip_inputs WHERE json_array_length(payload,'$.frames')>0 ORDER BY time_ms,rowid LIMIT 1").fetchone()
        first=json.loads(first_row[0]) if first_row else {'frames':[]}
        if source == 'UNKNOWN' and 'synthetic' in first.get('provenance','').lower():
            source = 'SYNTHETIC_DATA'
        markets = {f['market_id']: f['market'] for f in first['frames']}
        tips = [m['game_start_ms'] for m in markets.values() if type(m.get('game_start_ms')) is int]
        period = 'SYNTHETIC' if source == 'SYNTHETIC_DATA' else 'PRE_GAME' if tips and end < min(tips) else 'TIME_OVERLAPS_GAME_NOT_VERIFIED' if tips else 'UNKNOWN'
        policy = json.loads(db.execute('SELECT config FROM pe_control WHERE id=1').fetchone()[0])
        return {'id': digest(relative), 'path': relative, 'source': source, 'period': period,
                'start_ms': start, 'end_ms': end, 'duration_s': (end-start)/1000, 'input_records': n,
                'bytes': path.stat().st_size, 'markets': [{k: m.get(k) for k in ('market_id','question','game_start_ms')} for m in markets.values()],
                'research_phase': policy.get('research_phase', False), 'full_game_verified': False,
                'limitations': ['Input records include watchdog reuse, not unique market observations',
                                'Scheduled-tip comparison is not proof of actual game phase']}
    finally:
        db.close()


def catalog(root):
    root = Path(root).resolve()
    entries, errors = [], []
    # Do not recursively catalog derived experiment lanes or duplicate curriculum runs.
    paths = list((root/'data'/'console').glob('*.sqlite'))
    for folder in ('completion-stress', 'next-long'):
        paths += list((root/'data'/folder).glob('*.sqlite'))
    for path in sorted(paths):
        if path.is_symlink():
            continue
        try:
            item = inspect(root, path)
            if item:
                entries.append(item)
        except Exception as error:
            errors.append({'file': path.name, 'error': type(error).__name__})
    return {'samples': entries, 'errors': errors, 'real_submission_calls': 0}


def resolve(root, sample_id):
    match = next((x for x in catalog(root)['samples'] if x['id'] == sample_id), None)
    if not match:
        raise ValueError('Unknown registered sample; refresh catalog')
    return Path(root).resolve()/'data'/match['path'], match
