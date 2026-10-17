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

    def test_bounded_paper_exit_updates_same_fifo_and_fee_cashflow(self):
        from nba_recorder.inplay.paper_exit import execute
        fs=self.feed();self.om.advance(AT+10)
        buy=next(o for o in self.om.orders() if o['asset_id']=='a' and o['side']=='BUY' and o['state']=='OPEN')
        self.om.adapter.fill(buy['id'],'5',self.om.now,'explicit-test-buy');self.om.advance(AT+20)
        for o in self.om.orders():self.om.cancel(o['id'])
        self.om.advance(AT+40)
        f=self.om.prepare_submission_frame(None,fs[0]);before=self.om.account()['cash']
        result=execute(self.om,f,'5','.45','exit-test','.02')
        self.assertEqual(float(result['filled']),5);self.assertEqual(float(self.om.account()['positions']['a']),0)
        self.assertGreater(float(self.om.account()['cash']),float(before));self.assertTrue(self.om.consistent())
        self.assertEqual(self.om.real_submission_calls,0)
        with self.assertRaises(ValueError):execute(self.om,f,'5','.45','exit-test','.02')

    def test_taker_needs_persisted_authority(self):
        from nba_recorder.inplay.reference import submission_risk
        f=self.feed()[0];f['phase']='ACTIVE_MM'
        target={'side':'SELL','price':'.48','size':'5','maker_only':False,'purpose':'BOUNDED_PAPER_EXIT',
                'asset_id':'a','market_id':f['market_id'],'event_id':f['market']['event_id'],'expires_ms':AT+1000}
        self.assertIn('unauthorized_or_excess_paper_taker_exit',submission_risk(target,f,self.om.account(),[],self.policy,AT))

    def test_compatible_parameter_change_preserves_capital_on_restart(self):
        self.feed();before=self.om.account()['cash'];run=self.om.run_id
        capital=self.om.db.execute("SELECT count(*) FROM l6_events WHERE kind='CAPITAL'").fetchone()[0]
        self.om.change_research_parameters({'quote_distance':'.03','max_order_lifetime_ms':90000})
        saved=self.om.policy
        restored=UnifiedPaperManager(self.store,saved)
        self.assertEqual(restored.run_id,run);self.assertEqual(restored.account()['cash'],before)
        self.assertEqual(self.om.db.execute("SELECT count(*) FROM l6_events WHERE kind='CAPITAL'").fetchone()[0],capital)

    def test_auto_q4_once_and_report_failure_not_oms_action(self):
        import asyncio
        from types import SimpleNamespace
        from nba_recorder.console.service import ConsoleService
        f=frame(AT,period=4);self.om.phases.update(f,AT)
        service=ConsoleService(self.tmp.name);service.session_id=self.sid
        service.runner=SimpleNamespace(om=self.om,latest={'a':f})
        service.publish=lambda **kwargs:None
        calls=[];service.schedule_report=lambda *args:calls.append(args)
        async def run():
            await service.pump();await service.pump()
        before=self.om.account();asyncio.run(run())
        self.assertEqual(len(calls),1);self.assertEqual(self.om.account(),before)

    def test_watchdog_compression_keeps_interval_and_duplicate_guard(self):
        fs=self.feed();before=self.om.db.execute('SELECT count(*) FROM ip_inputs').fetchone()[0]
        for i in range(1,10):
            cooked=[{**f,'time_ms':AT+i} for f in fs]
            event={'id':'watch-'+str(i),'kind':'WATCHDOG','time_ms':AT+i,'frames':cooked,'trades':[]}
            self.session.on_event(event);self.assertFalse(self.session.on_event(event))
        after=self.om.db.execute('SELECT count(*) FROM ip_inputs').fetchone()[0]
        self.assertLess(after-before,9)
        self.assertGreater(self.om.db.execute("SELECT count FROM ip_diagnostic_intervals WHERE kind='watchdog'").fetchone()[0],1)

    def test_clock_review_cannot_be_bypassed_at_dispatch_boundary(self):
        f=frame(AT);self.om.phases.update(f,AT)
        corrected=frame(AT+1,remaining=650)
        self.assertEqual(self.om.phases.update(corrected,AT+1)['state'],'UNKNOWN')
        self.assertEqual(self.om.phases.boundary(corrected,AT+1),'UNKNOWN')
        reviewed=frame(AT+2,remaining=649)
        self.assertEqual(self.om.phases.update(reviewed,AT+2)['state'],'ACTIVE_MM')
