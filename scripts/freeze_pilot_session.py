"""Seal the actually processed Paper input stream for offline reproducible replay.

Reads immutable ip_inputs through a read-only connection, including WATCHDOG,
runtime guards and operator permissions; never re-creates disconnect history.
"""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nba_recorder.fair_value.store import read_connection
from nba_recorder.quote_engine.storage import PaperStore
from nba_recorder.fair_value.store import digest


def freeze_session(source, output):
    if Path(source).resolve() == Path(output).resolve():
        raise ValueError('separate replay store required')
    db = read_connection(source)
    try:
        events = [json.loads(r[0]) for r in db.execute('SELECT payload FROM ip_inputs ORDER BY time_ms,rowid')]
        if not events:
            raise ValueError('no recorded inputs; not a successful empty replay')
        buckets = {}
        for event in events:
            buckets.setdefault(event['time_ms'], []).append(event)
        # Preserve local receipt order inside a same-ms bucket. Do not shift
        # timestamps or pretend it proves exchange cross-channel ordering.
        sealed = [{'id': digest(['recorded-bucket', at, group]), 'time_ms': at,
                   'frames': [], 'trades': [], 'recorded_events': group}
                  for at, group in buckets.items()]
        store = PaperStore(output)
        try:
            identity = store.seal(sealed, {'mode': 'BOOK_DRIVEN', 'bucketed_recorded_events': True,
                                         'start_ms': events[0]['time_ms'], 'end_ms': events[-1]['time_ms'],
                                         'source_kind': 'actually processed public recording',
                                         'source': str(Path(source).name), 'network_calls': 0,
                                         'timing': 'received/processed/strategy-available preserved, no future reconstruction',
                                         'guard_policy': 'recorded operator/connection/epoch/watchdog outcomes preserved',
                                         'full_game_verified': False})
            return {'tape_id': identity, 'events': len(events), 'out': str(output), 'real_submission_calls': 0}
        finally:
            store.db.close()
    finally:
        db.close()


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--source', required=True)
    p.add_argument('--out', required=True)
    args = p.parse_args()
    print(json.dumps(freeze_session(args.source, args.out)))
