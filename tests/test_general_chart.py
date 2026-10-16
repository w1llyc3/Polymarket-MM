import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock
import httpx
from nba_recorder.api import parse_time, PublicAPI
from nba_recorder.discovery import normalize_market, discover
from nba_recorder.console.chart import query
from nba_recorder.inplay.demo import AT, frame
from nba_recorder.inplay.manager import UnifiedPaperManager
from nba_recorder.inplay.runner import PaperRunner
from nba_recorder.inplay.store import SingleWriterStore
from nba_recorder.inplay.reference import book_risk
from tests.test_core import fixture


class DiscoveryTests(unittest.TestCase):
    def test_other_full_game_slug_allowed_with_rule_proof_not_half_game(self):
        e,m,_=fixture();m['slug']='future-provider-game-moneyline'
        m['description']='The result is the final score including any overtime periods.'
        self.assertEqual(normalize_market(e,m,{'series':'10345'},0)[1],'MARKET_FOUND')
        m['gamePeriod']='first_half'
        self.assertEqual(normalize_market(e,m,{'series':'10345'},0)[1],'EXCLUDED_BY_FILTER')

    def test_invalid_custom_dates_rejected_before_scan_or_account_allocation(self):
        from nba_recorder.console.service import ConsoleService
        with tempfile.TemporaryDirectory() as tmp:
            s=ConsoleService(tmp)
            with self.assertRaises(ValueError):
                s.handle({'session_id':None,'expected_version':s.version,'action':'scan','season_type':'ALL','day_filter':'CUSTOM','custom_start':'2030-10-20','custom_end':'2031-01-01'})
            self.assertIsNone(s.scan_task);self.assertIsNone(s.runner)
    def test_all_types_cross_season_without_fixed_slug_or_date(self):
        for kind,label in [('PRESEASON','Preseason'),('REGULAR_SEASON','Regular Season'),('PLAY_IN','Play-In'),('PLAYOFFS','NBA Finals')]:
            with self.subTest(kind=kind):
                e,m,_=fixture();e.update(slug='new-provider-match-123',startTime='2030-06-01T23:00:00Z',seasonType=label,live=True)
                m['slug']=e['slug'];m['description']='including any overtime postponed canceled 50-50'
                item,status=normalize_market(e,m,{'series':'10345'},parse_time('2030-06-02T00:00:00Z'))
                self.assertEqual(status,'MARKET_FOUND');self.assertEqual(item['season'],'2029-30')
                self.assertEqual(item['season_type'],kind);self.assertEqual(item['game_phase'],'LIVE')
                self.assertFalse(item['paper_scope_approved'])
                if kind=='PLAYOFFS':self.assertEqual(item['competition_round'],'NBA Finals')

    def test_cup_tags_not_blanket_exclusion_and_unknown_is_readable(self):
        e,m,_=fixture();e['seasonType']='Regular Season';e['tags']=[{'label':'NBA Cup'}]
        item,status=normalize_market(e,m,{'series':'10345'},0)
        self.assertEqual(status,'MARKET_FOUND');self.assertEqual(item['competition_tags'],['NBA_CUP'])
        e.pop('seasonType');e['tags']=[]
        item,_=normalize_market(e,m,{'series':'10345'},0)
        self.assertEqual(item['season_type'],'UNKNOWN');self.assertTrue(item['collection_allowed'])
        e['sport']={'sport':'wnba'}
        self.assertEqual(normalize_market(e,m,{'series':'10345'},0)[1],'EXCLUDED_BY_FILTER')

    def test_identity_same_teams_time_conflict_and_wrong_outcome(self):
        e,m,_=fixture();m['gameStartTime']='2026-10-21T19:00:00Z'
        a,_=normalize_market(e,m,{'series':'10345'},0)
        self.assertTrue(a['identity_conflict']);self.assertFalse(a['collection_allowed'])
        m['outcomes']='["Celtics","76ers"]'
        self.assertEqual(normalize_market(e,m,{'series':'10345'},0)[1],'IDENTITY_AMBIGUOUS')

    def test_expanded_discovery_never_grants_phase_permission(self):
        f=frame();f['market']['discovery_schema']='NBA_GENERAL_V1';f['market']['paper_scope_approved']=False
        f['phase']='ACTIVE_MM'
        from nba_recorder.inplay.policy import BookPolicy
        self.assertIn('competition_paper_scope_not_approved',book_risk(f,BookPolicy(),AT))


class AsyncDiscovery(unittest.IsolatedAsyncioTestCase):
    async def test_all_categories_flow_through_dynamic_metadata_discovery(self):
        events=[]
        for i,label in enumerate(('Preseason','Regular Season','Play-In','NBA Finals')):
            e,m,_=fixture();e.update(id=str(i),slug='new-match-'+str(i),startTime='2030-10-20T23:00:00Z',seasonType=label,sport={'sport':'nba'})
            m.update(id=str(i),slug=e['slug']);e['markets']=[m];events.append(e)
        async def get(api,host,path,params=None):
            if path=='/sports':return [{'sport':'nba','series':'not-fixed','primaryTagId':99}]
            self.assertEqual(params['tag_id'],'99')
            return events
        response=httpx.Response(403,request=httpx.Request('GET','https://cdn.nba.com'))
        with patch.object(PublicAPI,'get',get),patch.object(httpx.AsyncClient,'get',AsyncMock(return_value=response)):
            api=PublicAPI()
            try:
                r=await discover(api,parse_time('2030-10-20T00:00:00Z'),parse_time('2030-10-21T00:00:00Z'))
                self.assertEqual({m['season_type'] for m in r['markets']},{'PRESEASON','REGULAR_SEASON','PLAY_IN','PLAYOFFS'})
                self.assertEqual(r['status'],'MARKET_FOUND')
                filtered=await discover(api,parse_time('2030-10-20T00:00:00Z'),parse_time('2030-10-21T00:00:00Z'),'PLAYOFFS')
                self.assertEqual(len(filtered['markets']),1)
            finally:await api.close()

    async def test_schedule_without_market_distinct_from_access_failure(self):
        official={'leagueSchedule':{'seasonYear':'2030-31','gameDates':[{'games':[{'gameId':'test-official-game','gameDateTimeUTC':'2030-10-20T23:00:00Z','homeTeam':{'teamTricode':'DET'},'awayTeam':{'teamTricode':'BOS'},'gameLabel':'Regular Season'}]}]}}
        async def get(api,host,path,params=None):
            return [{'sport':'nba','series':'dynamic','primaryTagId':99}] if path=='/sports' else []
        response=httpx.Response(200,json=official,request=httpx.Request('GET','https://cdn.nba.com'))
        with patch.object(PublicAPI,'get',get),patch.object(httpx.AsyncClient,'get',AsyncMock(return_value=response)):
            api=PublicAPI()
            try:
                r=await discover(api,parse_time('2030-10-20T00:00:00Z'),parse_time('2030-10-21T00:00:00Z'))
                self.assertEqual(r['status'],'NO_LISTED_MARKET');self.assertEqual(r['schedule_only'][0]['status'],'NO_LISTED_MARKET')
            finally:await api.close()
        with patch.object(PublicAPI,'get',AsyncMock(side_effect=httpx.ConnectError('offline'))),patch.object(httpx.AsyncClient,'get',AsyncMock(return_value=response)):
            api=PublicAPI()
            try:
                r=await discover(api,parse_time('2030-10-20T00:00:00Z'),parse_time('2030-10-21T00:00:00Z'))
                self.assertEqual(r['status'],'ACCESS_FAILED');self.assertEqual(r['schedule_only'][0]['status'],'ACCESS_FAILED')
            finally:await api.close()


class ChartTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'paper.sqlite'
        self.store=SingleWriterStore(self.path);self.om=UnifiedPaperManager(self.store,start_ms=AT);self.r=PaperRunner(self.om,'L1')
        self.om.split(frame()['market'],'5','synthetic-only')
        with self.om.db:self.om.db.execute("INSERT INTO ip_session_config VALUES('console_origin',?)",(json.dumps({'data_source':'SYNTHETIC_DATA','execution_source':'PAPER_EXECUTION'}),))
        self.feed(AT);self.feed(AT+1);self.om.advance(AT+7)

    def tearDown(self):self.store.close();self.tmp.cleanup()

    def feed(self,at,trades=()):
        with patch('nba_recorder.inplay.runner.utc_ms',return_value=at):self.r.feed([frame(at,a) for a in ('a','b')],trades,provenance='synthetic')

    def test_mid_band_gaps_asof_and_readonly_refresh(self):
        first=query(self.path,'a',AT,AT+7)
        self.assertTrue(first['prices']);self.assertEqual(first['prices'][-1]['mid'],'0.50')
        thin=[frame(AT+8,a) for a in ('a','b')]
        thin[0]['book']['bids'][0]['size']='1'
        with patch('nba_recorder.inplay.runner.utc_ms',return_value=AT+8):self.r.feed(thin,provenance='synthetic')
        thin_point=query(self.path,'a',AT,AT+8)['prices'][-1]
        self.assertEqual(thin_point['mid'],'0.50')
        self.assertIn('depth_drained',thin_point['reference_quality'])
        self.r.connection_guard=lambda:False;self.feed(AT+20)
        after=query(self.path,'a',AT,AT+20)
        self.assertIsNone(after['prices'][-1]['mid']);self.assertIn('disconnected',after['prices'][-1]['quality'])
        capital=self.om.db.execute("SELECT count(*) FROM l6_events WHERE kind='CAPITAL'").fetchone()[0]
        total=self.om.db.total_changes
        for _ in range(5):query(self.path,'b',AT,AT+20)
        self.assertEqual(self.om.db.total_changes,total)
        self.assertEqual(self.om.db.execute("SELECT count(*) FROM l6_events WHERE kind='CAPITAL'").fetchone()[0],capital)
        self.assertEqual(query(self.path,'a',AT,AT+7)['prices'],first['prices'])

    def test_late_fill_during_bad_data_updates_inventory_without_fabricated_pnl(self):
        buy=next(o for o in self.om.orders() if o['side']=='BUY' and o['asset_id']=='a')
        self.r.connection_guard=lambda:False
        self.feed(AT+8)
        self.assertTrue(self.om.adapter.fill(buy['id'],'2',AT+6,'late-after-gap',2,observed_ms=AT+9))
        self.om.advance(AT+11)
        q=query(self.path,'a',AT,AT+11)
        self.assertEqual(q['accounts'][-1]['inventory'],'7')
        self.assertIsNone(q['accounts'][-1]['unrealized'])
        self.assertTrue(self.om.consistent())

    def test_accepted_orders_partial_cancel_unknown_and_historical_ledger(self):
        buy=next(o for o in self.om.orders() if o['side']=='BUY' and o['asset_id']=='a')
        before=query(self.path,'a',AT,AT+7)
        self.om.adapter.fill(buy['id'],'2',AT+8,'synthetic-partial',2,observed_ms=AT+8)
        self.om.advance(AT+10)
        self.feed(AT+11)
        self.om.cancel(buy['id'],drop_response=True)
        q=query(self.path,'a',AT,AT+12)
        self.assertTrue(any(o['accepted_evidence'] and o['authority_state']=='OPEN' for o in q['orders']))
        self.assertTrue(any(o['state']=='CANCEL_PENDING' for o in q['orders']))
        self.assertEqual(q['paper_fills'][0]['quantity'],'2')
        self.assertEqual(before['accounts'][-1]['inventory'],'5')
        self.assertEqual(q['accounts'][-1]['inventory'],'7')
        self.assertIsNotNone(q['accounts'][-1]['realized'])
        self.assertEqual(query(self.path,'a',AT,AT+7)['paper_fills'],[])
        pending=next(o for o in self.om.orders() if o['side']=='SELL' and o['asset_id']=='a' and o['state']=='OPEN')
        self.om.cancel(pending['id'],drop_response=True)
        self.om.advance(self.om.now+self.om.policy.request_timeout_ms+1)
        uncertain=query(self.path,'a',AT,self.om.now)
        self.assertTrue(any(o['state']=='UNKNOWN' for o in uncertain['orders']))
        self.assertEqual(self.om.order(pending['id'])['state'],'UNKNOWN')

    def test_unaccepted_target_not_evidence_and_cursor_no_silent_truncation(self):
        q=query(self.path,'a',AT,AT+7,limit=1)
        self.assertTrue(q['has_more'])
        more=query(self.path,'a',AT,AT+7,q['cursors'],limit=1)
        self.assertGreater(more['cursors']['input'],q['cursors']['input'])
        all_=query(self.path,'a',AT,AT+7)
        self.assertTrue(all(not o['accepted_evidence'] for o in all_['orders'] if o['kind'] in {'CREATED','QUEUED','SUBMIT_REQUEST'}))
        self.assertEqual(all_['data_source'],'SYNTHETIC_DATA')
        self.assertEqual(all_['execution_source'],'PAPER_EXECUTION')

    def test_public_prints_are_separate_from_paper_and_unknown_aggressor(self):
        trade={'event_id':'public-1','asset_id':'b','condition_id':'synthetic-condition','side':'BUY','price':'.55','size':'3','exchange_ms':AT+8}
        self.feed(AT+10,[trade])
        q=query(self.path,'b',AT,AT+10)
        self.assertEqual(q['public_trades'][0]['aggressor_side'],'UNKNOWN')
        self.assertEqual(q['public_trades'][0]['execution'],'PUBLIC_MARKET_PRINT')
        self.assertEqual(query(self.path,'a',AT,AT+10)['public_trades'],[])

    def test_ui_has_one_canvas_and_no_strategy_calls_in_chart_code(self):
        root=Path(__file__).resolve().parents[1]/'nba_recorder/console/static'
        self.assertEqual((root/'index.html').read_text(encoding='utf-8').count('<canvas '),1)
        js=(root/'chart.js').read_text(encoding='utf-8')
        self.assertNotIn('/api/command',js);self.assertIn('valid_until_ms',js);self.assertIn('localStorage',js)
