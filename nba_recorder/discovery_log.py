"""Compact discovery audit; market recording remains in the source databases."""
import gzip
import json
import os
import time
import uuid
from pathlib import Path


class DiscoveryLog:
    # Every poll retains normalized markets, errors, endpoints and timestamps.
    # Large unfiltered responses are sampled, explicitly, rather than repeated.
    def __init__(self, directory, raw_interval_ms=3_600_000, change_interval_ms=300_000):
        if raw_interval_ms <= 0 or change_interval_ms <= 0:
            raise ValueError('positive discovery audit intervals required')
        self.directory = Path(directory)
        self.raw_interval_ms = raw_interval_ms
        self.change_interval_ms = change_interval_ms
        self.cursor_path = self.directory / 'discovery-log-state.json'
        self.cursor = json.loads(self.cursor_path.read_text(encoding='utf-8')) if self.cursor_path.exists() else {}

    def append(self, scan):
        at = scan.get('scanned_at_ms', int(time.time() * 1000))
        previous = self.cursor.get('snapshot')
        # The cursor holds a basename, never an arbitrary filesystem path.
        exists = isinstance(previous, str) and Path(previous).name == previous and (self.directory / 'discovery-snapshots' / previous).is_file()
        elapsed = at - self.cursor.get('scanned_at_ms', at)
        reason = ('FIRST_OR_MISSING_SNAPSHOT' if not exists else
                  'PERIODIC' if elapsed >= self.raw_interval_ms else
                  'STATUS_CHANGED' if scan.get('status') != self.cursor.get('status') and elapsed >= self.change_interval_ms else None)
        summary = {k: v for k, v in scan.items() if k != 'raw_events'}
        summary.update(schema='DISCOVERY_SUMMARY_V2', scanned_at_ms=at,
                       raw_event_count=len(scan.get('raw_events', [])),
                       raw_response_saved=reason is not None,
                       raw_snapshot=None, raw_snapshot_reason=reason,
                       raw_response_policy='First scan; hourly; status change after 5 minute cooldown. Other polls retain normalized metadata only.')
        cursor = self.cursor
        if reason:
            folder = self.directory / 'discovery-snapshots'
            folder.mkdir(exist_ok=True)
            name = f'{at}-{uuid.uuid4().hex}.json.gz'
            temporary = folder / (name + '.tmp')
            try:
                with temporary.open('xb') as raw:
                    with gzip.GzipFile(fileobj=raw, mode='wb', compresslevel=1, mtime=0) as compressed:
                        for chunk in json.JSONEncoder(ensure_ascii=False, separators=(',', ':')).iterencode(scan):
                            compressed.write(chunk.encode('utf-8'))
                    raw.flush()
                    os.fsync(raw.fileno())
                temporary.replace(folder / name)
            finally:
                if temporary.exists():
                    temporary.unlink()
            summary['raw_snapshot'] = 'discovery-snapshots/' + name
            cursor = {'snapshot': name, 'scanned_at_ms': at, 'status': scan.get('status')}
        # A new filename keeps legacy full-response JSONL untouched and readable.
        with (self.directory / 'discovery-summary.jsonl').open('a', encoding='utf-8') as fp:
            fp.write(json.dumps(summary, ensure_ascii=False, separators=(',', ':')) + '\n')
        if cursor is not self.cursor:
            temporary = self.cursor_path.with_suffix('.tmp')
            temporary.write_text(json.dumps(cursor), encoding='utf-8')
            temporary.replace(self.cursor_path)
            self.cursor = cursor
        return summary
