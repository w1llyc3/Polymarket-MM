"""Synthetic identity/lifecycle tests, not real full-game acceptance."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock
import httpx
from nba_recorder.api import select_market, parse_time, PublicAPI
from nba_recorder.game_state import information
from nba_recorder.inplay.sports import normalize
from nba_recorder.inplay.demo import AT, frame
from nba_recorder.inplay.runner import PaperRunner
from nba_recorder.inplay.manager import UnifiedPaperManager
from nba_recorder.inplay.store import SingleWriterStore
from nba_recorder.console.service import ConsoleService
from nba_recorder.pilot import PilotWindow
from nba_recorder.preflight import preflight, EvidenceAPI
from tests.test_core import fixture


def preseason():
    e, m, _ = fixture()
    e.update(slug='nba-bos-phi-2026-10-16', startTime='2026-10-16T23:00:00Z', gameId=20023840)
    e['teams'][1].update(name='76ers', abbreviation='phi')
    m.update(slug=e['slug'], outcomes='["Celtics","76ers"]', description='Including any overtime. If postponed wait. If canceled resolve 50-50.')
    return e, m


class IdentityTests(unittest.TestCase):
    def selected(self):
        e, m = preseason()
        return select_market(e, m, '10345', 0, scope='full-game', research=True)[0]

    def test_explicit_research_preserves_regular_default_and_season(self):
        e, m = preseason()
        self.assertIsNone(select_market(e, m, '10345', 0)[0])
        selected = self.selected()
        self.assertEqual(selected['season_type'], 'PRESEASON')
        self.assertEqual(selected['nba_game_id'], '0012600018')
        self.assertTrue(selected['rules_verified'])
        self.assertEqual(fixture()[2]['season_type'], 'REGULAR_SEASON')

    def test_same_teams_different_date_not_same_verified_game(self):
        e, m = preseason()
        e.update(startTime='2026-10-15T23:00:00Z', slug='nba-bos-phi-2026-10-15')
        m['slug'] = e['slug']
        selected, _ = select_market(e, m, '10345', 0, scope='full-game', research=True)
        self.assertEqual(selected['season_type'], 'UNKNOWN')
        self.assertIsNone(selected['nba_game_id'])

    def test_live_discovery_independent_of_season_or_quote_permission(self):
        e, m = preseason()
        e['live'] = True
        selected, _ = select_market(e, m, '10345', parse_time('2026-10-17T00:00:00Z'), scope='full-game', research=True)
        self.assertEqual(selected['game_phase'], 'LIVE')
        self.assertEqual(selected['season_type'], 'PRESEASON')
        raw = {'gameId':20023840,'leagueAbbreviation':'NBA','homeTeam':'76ers','awayTeam':'Celtics','score':'12-10','period':'1Q','elapsed':'08:00','status':'InProgress','live':True,'ended':False}
        info = information(raw, selected, 100, 110)
        self.assertTrue(info['display_eligible'])
        self.assertEqual(info['score'], '12-10')
        self.assertIsNone(info['upstream_timestamp_ms'])
        self.assertFalse(info['quote_authorized'])
        self.assertIsNone(normalize(raw, 100)['identity_evidence'])

    def test_wrong_roles_or_game_id_fail_information_mapping(self):
        m = self.selected()
        raw = {'gameId':20023841,'leagueAbbreviation':'NBA','homeTeam':'Celtics','awayTeam':'76ers','score':'0-0'}
        info = information(raw, m, 100, 110)
        self.assertFalse(info['display_eligible'])
        self.assertIsNone(info['score'])
        self.assertTrue(info['conflict'])

    def test_missing_fields_remain_unknown_and_clock_does_not_tick(self):
        m = self.selected()
        raw = {'gameId':20023840,'leagueAbbreviation':'NBA','homeTeam':'76ers','awayTeam':'Celtics'}
        info = information(raw, m, 100, 110)
        self.assertIsNone(info['score'])
        self.assertIsNone(info['period'])
        self.assertIsNone(info['clock_raw'])
        self.assertEqual(info['clock_semantics'], 'UNVERIFIED')

    def test_outcome_mapping_cannot_reverse_or_invent_teams(self):
        e, m = preseason()
        m['outcomes'] = '["Celtics","Pistons"]'
        self.assertEqual(select_market(e,m,'10345',0,scope='full-game',research=True)[1], 'ambiguous_outcome_team_mapping')

    def test_utc8_tomorrow_is_previous_exchange_date(self):
        self.assertEqual(self.selected()['game_start_ms'], parse_time('2026-10-17T07:00:00+08:00'))


class WindowTests(unittest.TestCase):
    def test_paper_stop_intent_survives_real_store_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'paper.sqlite'
            s = SingleWriterStore(path)
            om = UnifiedPaperManager(s,start_ms=AT)
            PaperRunner(om).stop_paper('test_explicit_stop')
            s.close()
            reopened = SingleWriterStore(path)
            try:
                restored = UnifiedPaperManager(reopened,start_ms=AT+1)
                runner = PaperRunner(restored)
                self.assertTrue(runner.paper_stopped)
                self.assertFalse(runner.quotes_enabled)
                self.assertTrue(restored.halted)
                self.assertEqual(restored.real_submission_calls,0)
            finally:
                reopened.close()

    def test_same_ms_replay_seal_preserves_original_order_and_timestamps(self):
        from scripts.freeze_pilot_session import freeze_session
        from nba_recorder.storage import encode
        from nba_recorder.quote_engine.storage import PaperStore
        with tempfile.TemporaryDirectory() as tmp:
            source, out = Path(tmp)/'paper.sqlite', Path(tmp)/'tape.sqlite'
            s = SingleWriterStore(source)
            try:
                UnifiedPaperManager(s,start_ms=AT)
                with s.db:
                    for key in ('first','second'):
                        s.db.execute('INSERT INTO ip_inputs VALUES(?,?,?)',(key,AT,encode({'id':key,'time_ms':AT,'frames':[],'trades':[]})))
            finally:
                s.close()
            result = freeze_session(source,out)
            sealed = PaperStore.readonly(out)
            try:
                manifest = sealed.verify(result['tape_id'])
                bucket = json.loads(sealed.db.execute('SELECT payload FROM qe_input').fetchone()[0])
                self.assertTrue(manifest['bucketed_recorded_events'])
                self.assertEqual([e['id'] for e in bucket['recorded_events']],['first','second'])
                self.assertTrue(all(e['time_ms']==AT for e in bucket['recorded_events']))
            finally:
                sealed.close()

    def test_report_counts_unique_books_not_watchdog_or_trade_reuse(self):
        from nba_recorder.storage import Store, encode
        from nba_recorder.pilot import research_report
        from dataclasses import asdict
        with tempfile.TemporaryDirectory() as tmp:
            s = SingleWriterStore(Path(tmp)/'paper.sqlite')
            source = Store(Path(tmp)/'source.sqlite')
            try:
                om = UnifiedPaperManager(s,start_ms=AT)
                f = frame(AT)
                with s.db:
                    for key,kind in (('original','SOURCE'),('reuse','SOURCE'),('tick','WATCHDOG')):
                        s.db.execute('INSERT INTO ip_inputs VALUES(?,?,?)',(key,AT,encode({'id':key,'time_ms':AT,'kind':kind,'frames':[f]})))
                source.db.executescript('CREATE TABLE sports_raw(id INTEGER); CREATE TABLE game_information(id INTEGER);')
                result={'policy':asdict(om.policy),'recording_coverage':{},'runner':{'issues':[]},'execution_evidence':{},'residual_inventory':{},'pnl_layers':{},'income_stages':{}}
                r=research_report(result,source.db,s.db)
                self.assertEqual(r['asset_book_samples'],1)
                self.assertEqual(r['quality_valid_samples'],1)
                self.assertEqual(r['book_sample_availability'],1)
            finally:
                s.close()
                source.close()

    def test_end_latches_paper_stop_while_collection_can_continue(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = SingleWriterStore(Path(tmp)/'paper.sqlite')
            try:
                om = UnifiedPaperManager(s,start_ms=AT)
                r = PaperRunner(om)
                w = PilotWindow(AT)
                r.observe_game_information({'display_eligible':True,'game_finished':True,'received_ms':AT},w,AT)
                self.assertTrue(r.paper_stopped)
                self.assertFalse(r.quotes_enabled)
                self.assertTrue(om.halted)
                self.assertFalse(r.stopping.is_set())
                r.observe_game_information({'display_eligible':True,'game_finished':False,'received_ms':AT+1},w,AT+1)
                self.assertTrue(r.paper_stopped)
                self.assertIsNone(w.stop_reason(AT+1))
                self.assertEqual(s.db.execute("SELECT count(*) FROM l6_events WHERE kind='SETTLEMENT'").fetchone()[0],0)
            finally:
                s.close()

    def test_stop_paper_preserves_collection_and_forbids_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = SingleWriterStore(Path(tmp)/'paper.sqlite')
            try:
                service = ConsoleService(tmp)
                service.runner = PaperRunner(UnifiedPaperManager(s,start_ms=AT))
                service.task = SimpleNamespace(done=lambda:False)
                service.lifecycle,service.mode = 'RUNNING','REAL_PAPER'
                def command(action):
                    return service.handle({'session_id':None,'expected_version':service.version,'action':action,'confirmed':True})
                command('stop_paper')
                self.assertEqual(service.lifecycle,'RUNNING')
                self.assertFalse(service.runner.stopping.is_set())
                command('reconcile')
                with self.assertRaises(ValueError):command('resume')
                self.assertFalse(service.runner.quotes_enabled)
            finally:
                s.close()

    def test_overtime_no_finish_and_hard_limit_not_complete(self):
        w = PilotWindow(0)
        w.observe({'display_eligible':True,'game_finished':False,'received_ms':12000000,'period':'OT'},12000000)
        self.assertIsNone(w.stop_reason(12000000))
        self.assertEqual(w.stop_reason(14400000), 'HARD_LIMIT_NOT_COMPLETE')
        self.assertEqual(w.coverage(14400000,w.stop_reason(14400000))['status'], 'NOT_COMPLETE')

    def test_fresh_confirmed_end_tail_not_market_settlement(self):
        w = PilotWindow(0)
        w.observe({'display_eligible':True,'game_finished':True,'received_ms':2000000},2000000)
        self.assertIsNone(w.stop_reason(2000000+599999))
        self.assertEqual(w.stop_reason(2600000), 'OBSERVED_GAME_END_PLUS_TAIL')
        self.assertNotIn('settlement', w.__dict__)

    def test_stale_conflicting_or_market_closed_does_not_finish_game(self):
        for info in ({'display_eligible':True,'game_finished':True,'received_ms':1},
                     {'display_eligible':True,'game_finished':True,'received_ms':100000,'conflict':True},
                     {'display_eligible':True,'game_finished':False,'market_closed':True,'received_ms':100000}):
            w = PilotWindow(0)
            w.observe(info,100000)
            self.assertIsNone(w.finished_ms)

    def test_over_thirty_minutes_one_account_continues_without_capital(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = SingleWriterStore(Path(tmp)/'paper.sqlite')
            try:
                om = UnifiedPaperManager(s,start_ms=AT)
                r = PaperRunner(om)
                r.quotes_enabled = False
                w = PilotWindow(AT)
                capital = s.db.execute("SELECT count(*) FROM l6_events WHERE kind='CAPITAL'").fetchone()[0]
                for at in (AT, AT+1, AT+1900000):
                    with patch('nba_recorder.inplay.runner.utc_ms',return_value=at):
                        r.feed([frame(at,a) for a in ('a','b')])
                    r.tick(at)
                    self.assertIsNone(w.stop_reason(at))
                self.assertEqual(s.db.execute("SELECT count(*) FROM l6_events WHERE kind='CAPITAL'").fetchone()[0],capital)
                self.assertEqual(len(om.orders()),0)
                self.assertEqual(om.real_submission_calls,0)
                self.assertGreater(om.now-AT,1800000)
            finally:
                s.close()


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_console_research_catalog_and_real_start_parameters(self):
        e,m=preseason();e['markets']=[m]
        async def get(api,host,path,params=None):
            if path=='/sports':return [{'sport':'nba','series':'10345','primaryTagId':745}]
            self.assertEqual(params['tag_id'],'745')
            self.assertNotIn('series_id',params)
            return [e]
        now=parse_time('2026-10-16T04:00:00Z')
        with tempfile.TemporaryDirectory() as tmp, patch.object(PublicAPI,'get',get), patch('nba_recorder.console.service.utc_ms',return_value=now):
            service=ConsoleService(tmp)
            await service.scan('CURRENT','PRESEASON','TOMORROW')
            self.assertEqual(service.catalog['markets'][0]['season_type'],'PRESEASON')
            self.assertEqual(service.catalog['window_start_ms'],parse_time('2026-10-16T16:00:00Z'))
            captured=[]
            async def run(*args,**kwargs):captured.append(kwargs);return {}
            with patch('nba_recorder.console.service.run_paper',run):
                service.handle({'session_id':None,'expected_version':service.version,'action':'start','mode':'OBSERVATION','market_id':'2','confirmed':True,'limit_s':14400})
                await service.task
            self.assertTrue(captured[0]['research'])
            self.assertEqual(captured[0]['seconds'],14400)
            self.assertEqual(service.limit_s,14400)

    async def test_preflight_access_failure_not_no_market_and_schema_separate(self):
        for error, expected in ((httpx.ConnectError('offline'),'ACCESS_FAILED'),(ValueError('schema'),'SCHEMA_UNSUPPORTED')):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as tmp, patch.object(EvidenceAPI,'scan',AsyncMock(side_effect=error)), patch.object(httpx.AsyncClient,'get',AsyncMock(return_value=httpx.Response(403))):
                result=await preflight('2026-10-16T16:00:00Z','2026-10-17T16:00:00Z',Path(tmp)/'preflight.json')
                self.assertEqual(result['status'],expected)

    async def test_preflight_success_empty_is_no_matching_market(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(EvidenceAPI,'scan',AsyncMock(return_value={'markets':[]})), patch.object(httpx.AsyncClient,'get',AsyncMock(return_value=httpx.Response(403))):
            r=await preflight('2026-10-16T16:00:00Z','2026-10-17T16:00:00Z',Path(tmp)/'preflight.json')
            self.assertEqual(r['status'],'NO_MATCHING_MARKET')
