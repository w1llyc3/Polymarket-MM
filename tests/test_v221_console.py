"""Synthetic full OMS/ledger regressions; no real game or execution claims."""
import asyncio
import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from nba_recorder.api import PublicAPI, parse_time
from nba_recorder.console.service import ConsoleService
from nba_recorder.inplay.demo import AT, frame
from nba_recorder.inplay.manager import UnifiedPaperManager
from nba_recorder.inplay.runner import PaperRunner
from nba_recorder.inplay.store import SingleWriterStore
from tests.test_core import fixture


class PauseLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = ConsoleService(self.tmp.name)
        self.store = SingleWriterStore(Path(self.tmp.name)/'paper.sqlite')
        self.om = UnifiedPaperManager(self.store, start_ms=AT)
        self.runner = PaperRunner(self.om, 'L1')
        self.service.runner = self.runner
        self.service.task = SimpleNamespace(done=lambda: False)
        self.service.lifecycle, self.service.mode = 'RUNNING', 'REAL_PAPER'
        self.om.split(frame(AT)['market'], '5', 'synthetic-capital')
        self.feed(AT)
        self.feed(AT+1)
        self.om.advance(AT+7)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def feed(self, at):
        with patch('nba_recorder.inplay.runner.utc_ms', return_value=at):
            self.runner.feed([frame(at, a) for a in ('a', 'b')])

    def command(self, action):
        return self.service.handle({'session_id': None, 'expected_version': self.service.version,
                                    'action': action, 'confirmed': True})

    def submissions(self):
        return self.om.db.execute("SELECT count(*) FROM pe_journal WHERE kind='SUBMIT_REQUEST'").fetchone()[0]

    def test_pause_ticks_real_reconcile_ticks_explicit_resume_with_inventory(self):
        self.command('pause')
        before = self.submissions()
        for at in (AT+20, AT+30, AT+40):
            self.feed(at)
            self.runner.tick(at)
            self.assertIsNotNone(self.om.mark_equity(list(self.runner.latest.values())))
        self.assertTrue(self.command('reconcile')['consistent'])
        self.assertEqual(self.om.risk_state, 'READY_FOR_MANUAL_RESET')
        for at in (AT+50, AT+60):
            self.feed(at)
            self.runner.tick(at)
            self.assertEqual(self.om.risk_state, 'READY_FOR_MANUAL_RESET')
            self.service.publish(force=True)
            self.service.read()
        self.assertEqual(self.submissions(), before)
        self.assertFalse(self.runner.quotes_enabled)
        self.command('resume')
        self.assertTrue(self.runner.quotes_enabled)
        self.assertEqual(self.om.risk_state, 'ACTIVE')
        self.assertTrue(self.om.consistent())
        self.assertEqual(self.om.real_submission_calls, 0)

    def test_pause_keeps_causal_late_partial_fill_and_cash(self):
        buy = next(o for o in self.om.orders() if o['side'] == 'BUY' and o['asset_id'] == 'a')
        before = self.om.account()
        self.command('pause')
        self.assertTrue(self.om.adapter.fill(buy['id'], '2', AT+6, 'late-before-cancel', 2, observed_ms=AT+8))
        self.om.advance(AT+30)
        from decimal import Decimal
        after = self.om.account()
        self.assertEqual(Decimal(after['positions']['a']), Decimal(before['positions']['a'])+2)
        self.assertEqual(Decimal(after['cash']), Decimal(before['cash'])-2*Decimal(buy['price']))
        self.assertTrue(self.om.consistent())

    def reject_fault(self, fault):
        self.command('pause')
        self.om.advance(AT+30)
        self.command('reconcile')
        self.runner.connection_guard = lambda: fault != 'disconnect'
        if fault == 'stale':
            for f in self.runner.latest.values():
                f['book_received_ms'] = AT-100000
        if fault == 'closed':
            for f in self.runner.latest.values():
                f['sports'].update(period=4, remaining_s=0)
        if fault == 'unknown':
            with self.om.db:
                order = self.om.orders()[0]
                order['state'] = 'UNKNOWN'
                self.om._save(order, 'SYNTHETIC_UNKNOWN_INJECTION')
        with self.assertRaises(ValueError):
            self.command('resume')
        self.assertFalse(self.runner.quotes_enabled)

    def test_resume_rejects_physical_disconnect(self):
        self.reject_fault('disconnect')

    def test_resume_rejects_stale_book(self):
        self.reject_fault('stale')

    def test_resume_rejects_closed_phase(self):
        self.reject_fault('closed')

    def test_resume_rejects_unknown_order(self):
        self.reject_fault('unknown')

    def test_operator_pause_rechecked_at_final_dispatch(self):
        from nba_recorder.inplay.reference import book_targets, book_data_risk
        f = self.runner.session.observe(self.runner.latest['a'])
        target = book_targets(f, self.om.account(), [], {}, self.om.policy)['targets'][0]
        key = self.om.enqueue('operator-gate', target, f)
        before = self.submissions()
        self.runner.quotes_enabled = False
        self.assertEqual(book_data_risk(self.runner.authorize(f), self.om.policy, self.om.now), [])
        self.assertEqual(self.om.dispatch(), 0)
        self.assertEqual(self.submissions(), before)
        self.assertEqual(self.om.order(key)['state'], 'REJECTED')


class Discovery(unittest.IsolatedAsyncioTestCase):
    async def test_bounded_catalog_and_start_chain(self):
        event, market, _ = fixture()
        market['description']='Final score including any overtime; postponed; canceled; 50-50.'
        now = parse_time('2026-10-20T18:00:00Z')
        rows = []
        for key, live, ended, kind in [('scheduled', False, False, 'moneyline'), ('live', True, False, 'moneyline'), ('ended', False, True, 'moneyline'), ('wrong', True, False, 'spread')]:
            e, m = copy.deepcopy(event), copy.deepcopy(market)
            e.update(id=key, live=live, ended=ended)
            if live or ended:
                e['startTime'] = '2026-10-20T17:00:00Z'
            m.update(id=key, sportsMarketType=kind)
            e['markets'] = [m]
            rows.append(e)
        requests = []
        async def get(api, host, path, params=None):
            if path == '/sports':
                return [{'sport': 'nba', 'series': '10345'}]
            self.assertEqual(path, '/events')
            requests.append(params)
            return rows
        with tempfile.TemporaryDirectory() as tmp, patch.object(PublicAPI, 'get', get), patch('nba_recorder.console.service.utc_ms', return_value=now):
            service = ConsoleService(tmp)
            await service.scan()
            self.assertEqual({m['market_id'] for m in service.catalog['markets']}, {'scheduled', 'live'})
            self.assertIn('start_time_min', requests[0])
            self.assertIn('start_time_max', requests[0])
            captured = []
            async def session(m):
                captured.append(m['market_id'])
            service.session = session
            service.handle({'session_id': None, 'expected_version': service.version, 'action': 'start', 'mode': 'REAL_PAPER', 'market_id': 'live', 'confirmed': True})
            await service.task
            sid = service.session_id
            await service.scan('HISTORY')
            self.assertEqual(service.session_id, sid)
            self.assertEqual(captured, ['live'])
            self.assertIn('ended', {m['market_id'] for m in service.catalog['markets']})
            with self.assertRaises(ValueError):
                service.handle({'session_id': sid, 'expected_version': service.version, 'action': 'start', 'mode': 'REAL_PAPER', 'market_id': 'ended', 'confirmed': True})
