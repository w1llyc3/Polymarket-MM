"""Explicit synthetic offline acceptance; never starts a collector or real order."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nba_recorder.research.engine import experiment
from nba_recorder.research.jobs import plot, register
from nba_recorder.fair_value.store import read_connection


def run(root):
    identity = uuid.uuid4().hex
    output = root / 'data/research' / identity
    result = experiment(root, output, synthetic=True, distances=('.02',), budget='25', split_shares='5')
    register(root, output)
    checks = []
    for lane in result['lanes']:
        with closing(read_connection(output/lane['lane']/'paper.sqlite')) as db:
            count = db.execute('SELECT count(*) FROM ip_inputs').fetchone()[0]
            payload = db.execute('SELECT payload FROM ip_inputs ORDER BY time_ms,rowid LIMIT 1').fetchone()[0]
            assert json.loads(payload)['provenance'] == 'synthetic'
            assert db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
        chart = plot(root, identity, lane['lane'])
        assert count and chart['total_points'] and lane['shutdown_ledger_consistent']
        checks.append({'lane': lane['lane'], 'input_records': count,
                       'plot_points': chart['total_points'], 'ledger_consistent': True})
    with closing(sqlite3.connect(output/'shared-inputs.sqlite')) as db:
        blobs = db.execute('SELECT count(*) FROM inputs').fetchone()[0]
    assert blobs == checks[0]['input_records']
    reports = []
    for p in (root/'data/storage-workflow').glob('*.json'):
        report = json.loads(p.read_text(encoding='utf-8'))
        if report['results'] and all(identity in r['path'] for r in report['results']):
            assert not report['errors']
            reports.append(p.relative_to(root).as_posix())
    assert reports, 'Enabled automatic completion hook must produce verified compression report'
    return {'source': 'SYNTHETIC_OFFLINE_ACCEPTANCE_NOT_A_GAME', 'experiment': identity,
            'models': checks, 'shared_blobs': blobs, 'compression_reports': reports,
            'real_submission_calls': result['real_submission_calls'], 'live_go': False}


if __name__ == '__main__':
    root = Path(__file__).resolve().parents[1]
    result = run(root)
    evidence = root/'evidence/storage-workflow'
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence/'synthetic-acceptance.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result, indent=2))
