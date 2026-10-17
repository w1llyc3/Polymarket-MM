import copy
import json
import tempfile
import unittest
from pathlib import Path
from dataclasses import replace
from nba_recorder.inplay.demo import AT, frame
from nba_recorder.inplay.policy import BookPolicy
from nba_recorder.inplay.manager import UnifiedPaperManager
from nba_recorder.inplay.store import SingleWriterStore
from nba_recorder.inplay.session import BookSession
from nba_recorder.inplay.reference import observed_mid, book_targets
from nba_recorder.console.research import distribution, markouts, exit_depth, analyze
from nba_recorder.console.history import resolve, view, save_report


class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.sid='e'*32;self.path=Path(self.tmp.name)/(self.sid+'.sqlite')
        self.store=SingleWriterStore(self.path);self.policy=BookPolicy(research_phase=True)
        self.om=UnifiedPaperManager(self.store,self.policy,AT);self.session=BookSession(self.om,'L1')
        with self.om.db:self.om.db.execute("INSERT INTO ip_session_config VALUES('console_origin',?)",(json.dumps({'data_source':'SYNTHETIC_DATA','execution_source':'PAPER_EXECUTION'}),))

    def tearDown(self):self.store.close();self.tmp.cleanup()

    def feed(self,at=AT):
        fs=[frame(at,a) for a in ('a','b')]
        for f in fs:
            f['market'].update(discovery_schema='NBA_GENERAL_V1',rules_verified=True,research_authorized=True,paper_scope_approved=False,season_type='UNKNOWN')
            f['sports'].update(observed_ms=None,paused=None)
        self.session.on_event({'id':str(at),'time_ms':at,'frames':fs,'trades':[]})
        return fs

    def test_research_unknown_category_missing_upstream_time_can_quote(self):
        self.feed();self.om.advance(AT+10)
        self.assertTrue(any(o['state']=='OPEN' for o in self.om.orders()))
        self.assertEqual(self.om.real_submission_calls,0)

    def test_thin_book_mid_valuation_and_size(self):
        f=self.feed()[0];f['book']['bids'][0]['size']='4';f['phase']='ACTIVE_MM'
        self.assertIsNotNone(observed_mid(f,self.policy,AT))
        decision=book_targets(f,self.om.account(),[],{},self.policy)
        self.assertFalse(any(t['side']=='BUY' and float(t['size'])>4 for t in decision['targets']))

    def test_current_data_revalidates_order_without_old_snapshot_expiry(self):
        self.feed();self.om.advance(AT+10);ids={o['id'] for o in self.om.orders() if o['state']=='OPEN'}
        for offset in range(1000,6000,1000):self.feed(AT+offset)
        self.assertTrue(ids & {o['id'] for o in self.om.orders() if o['state']=='OPEN'})

    def test_stale_cancels_but_not_permanent_account_halt(self):
        self.feed();self.om.advance(AT+10);f=frame(AT);f['time_ms']=AT+4000
        self.om.advance(AT+4000);self.session.observe(f)
        self.assertFalse(self.om.halted)
        self.assertTrue(all(o['state'] not in {'OPEN','PARTIALLY_FILLED'} for o in self.om.orders() if o['asset_id']==f['asset_id']))

    def test_closed_latch_manual_cannot_reopen(self):
        f=self.feed()[0]
        with self.om.db:
            self.om.db.execute("INSERT INTO ip_session_config VALUES('research_phase_grant',?)",(json.dumps({'market_id':f['market_id'],'state':'MM_CLOSED','period':4,'confirmed_ms':AT,'expires_ms':AT+60000}),))
        self.session.observe(f)
        with self.om.db:self.om.db.execute("UPDATE ip_session_config SET payload=? WHERE id='research_phase_grant'",(json.dumps({'market_id':f['market_id'],'state':'ACTIVE_MM','period':4,'confirmed_ms':AT,'expires_ms':AT+60000}),))
        self.assertEqual(self.session.observe(f)['phase'],'MM_CLOSED')

    def test_distribution_conservation_duplicates_and_missing_quantity(self):
        ts=[{'id':'a','asset_id':'a','time_ms':1,'price':'.51','size':'2'}, {'id':'b','asset_id':'a','time_ms':1,'price':'.51','size':'3'}, {'id':'c','asset_id':'b','time_ms':1,'price':'.49','size':'7'}, {'id':'d','asset_id':'a','time_ms':1,'price':'.5'}]
        result=distribution(ts+ts,'a',0,2)
        self.assertEqual(result['total'],'5');self.assertEqual(result['excluded'],1)
        self.assertEqual(distribution(ts,'a',0,2,amount=True)['total'],'2.55')

    def test_markout_sign_units_pending_unknown_no_future(self):
        fills=[{'id':'f','time_ms':0,'price':'.5','quantity':'2','side':s} for s in ('BUY','SELL')]
        prices=[{'time_ms':0,'mid':'.51','valid_until_ms':1000},{'time_ms':1000,'mid':'.52','valid_until_ms':1000},{'time_ms':1001,'mid':'.9','valid_until_ms':9000}]
        out=markouts(fills,prices,1000,(1,3))
        self.assertEqual(out[0]['fill_markout_cents'],'2.00');self.assertEqual(out[2]['fill_markout_cents'],'-2.00')
        self.assertEqual(out[1]['status'],'PENDING')
        self.assertEqual(markouts(fills,prices[:1],3000,(3,))[0]['status'],'UNKNOWN')

    def test_multi_lot_exits_use_depth_once_and_reserve_pending(self):
        fs={a:frame(AT,a) for a in ('a','b')};fs['a']['book']['bids']=[{'price':'.48','size':'3'}]
        a={'positions':{'a':'10','b':'2'},'reserved_inventory':{'a':'4'},'cash':'1','available_cash':'1'}
        e=exit_depth(a,fs,AT)[0]
        self.assertEqual(e['direct']['executable_quantity'],'3');self.assertEqual(e['direct']['unfilled'],'3')
        self.assertIsNone(e['net_direct_cash'])

    def test_history_is_readonly_and_report_versions_immutable(self):
        self.feed();before=self.om.account();v=view(self.tmp.name,self.sid)
        self.assertTrue(v['read_only']);self.assertEqual(self.om.account(),before)
        one=save_report(self.tmp.name,self.sid,'Q4_MANUAL',AT,'first')
        path=Path(self.tmp.name)/(self.sid+'-report-'+one['report_id']+'.json');raw=path.read_bytes()
        self.feed(AT+1000);save_report(self.tmp.name,self.sid,'FINAL_UPDATE')
        self.assertEqual(raw,path.read_bytes());self.assertEqual(self.om.account()['cash'],before['cash'])

    def test_path_injection_rejected(self):
        with self.assertRaises(ValueError):resolve(self.tmp.name,'../paper')

    def test_analyze_asof_excludes_later_records(self):
        self.feed();before=analyze(self.path,AT);self.feed(AT+1000)
        after=analyze(self.path,AT)
        self.assertEqual(before,after)
