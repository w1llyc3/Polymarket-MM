import gzip
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nba_recorder.discovery_log import DiscoveryLog


class DiscoveryLogTests(unittest.TestCase):
    def scan(self, at, status='MARKET_FOUND'):
        return {'scanned_at_ms': at, 'started_ms': at - 10, 'status': status,
                'markets': [{'market_id': '123', 'tick_size': '.01', 'outcomes': ['YES', 'NO']}],
                'schedule_only': [], 'evidence': [{'http_status': 200, 'received_ms': at}],
                'raw_events': [{'id': '123', 'unrelated_markets': 'x' * 100_000}]}

    def test_repeated_polls_keep_metadata_without_repeating_raw_responses(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            legacy = root / 'discovery.jsonl'; legacy.write_bytes(b'legacy evidence\n')
            log = DiscoveryLog(root)
            original = self.scan(1000)
            first = log.append(original)
            for i in range(1, 30):
                item = log.append(self.scan(1000 + i * 120_000))
                self.assertFalse(item['raw_response_saved'])
                self.assertIsNone(item['raw_snapshot'])
                self.assertEqual(item['markets'], original['markets'])
            self.assertEqual(len(list((root / 'discovery-snapshots').glob('*.gz'))), 1)
            self.assertEqual(json.loads(gzip.decompress((root / first['raw_snapshot']).read_bytes())), original)
            self.assertLess((root / 'discovery-summary.jsonl').stat().st_size, 30_000)
            self.assertEqual(legacy.read_bytes(), b'legacy evidence\n')
            self.assertIn('raw_events', original)

    def test_restart_hourly_and_missing_snapshot(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); first = DiscoveryLog(root).append(self.scan(1000))
            self.assertFalse(DiscoveryLog(root).append(self.scan(2000))['raw_response_saved'])
            next_row = DiscoveryLog(root).append(self.scan(3_601_000))
            self.assertEqual(next_row['raw_snapshot_reason'], 'PERIODIC')
            self.assertNotEqual(first['raw_snapshot'], next_row['raw_snapshot'])
            (root / next_row['raw_snapshot']).unlink()
            self.assertTrue(DiscoveryLog(root).append(self.scan(3_602_000))['raw_response_saved'])

    def test_errors_always_recorded_and_status_snapshots_rate_limited(self):
        with tempfile.TemporaryDirectory() as d:
            log = DiscoveryLog(d); log.append(self.scan(1000))
            failed = self.scan(2000, 'ACCESS_FAILED'); failed['evidence'] = [{'error': 'PoolTimeout'}]
            row = log.append(failed)
            self.assertEqual(row['evidence'], failed['evidence'])
            self.assertFalse(row['raw_response_saved'])
            self.assertEqual(log.append(self.scan(301_000, 'ACCESS_FAILED'))['raw_snapshot_reason'], 'STATUS_CHANGED')
            self.assertFalse(log.append(self.scan(302_000))['raw_response_saved'])

    def test_failed_snapshot_does_not_claim_success_or_advance_cursor(self):
        with tempfile.TemporaryDirectory() as d:
            log = DiscoveryLog(d)
            with patch('nba_recorder.discovery_log.gzip.GzipFile', side_effect=OSError('disk full')):
                with self.assertRaises(OSError): log.append(self.scan(1000))
            self.assertFalse(log.cursor_path.exists())
            self.assertFalse((Path(d) / 'discovery-summary.jsonl').exists())
            self.assertFalse(list((Path(d) / 'discovery-snapshots').glob('*.tmp')))
            self.assertTrue(log.append(self.scan(2000))['raw_response_saved'])
