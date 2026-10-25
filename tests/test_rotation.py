import copy
import json
import tempfile
import unittest
from dataclasses import replace, asdict
from pathlib import Path
from nba_recorder.inplay.demo import AT, frame, events
from nba_recorder.inplay.policy import BookPolicy
from nba_recorder.inplay.manager import UnifiedPaperManager
from nba_recorder.inplay.store import SingleWriterStore
from nba_recorder.research.day_study import IndexedHistoricalSession
from nba_recorder.inplay.reference import book_targets, submission_risk
from nba_recorder.inplay.rotation import prices


def policy(**kw):
    return BookPolicy(initial_cash='25', research_phase=True, quote_profile='INPLAY_ROTATION_V1', **kw)


class RotationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/'paper.sqlite'
        self.store = SingleWriterStore(self.path)
        self.om = UnifiedPaperManager(self.store, policy(), AT)
        self.session = IndexedHistoricalSession(self.om, 'L2')

    def tearDown(self):
        self.store.close(); self.tmp.cleanup()

    def observed(self, at=AT, period=2, bid='.49', ask='.51'):
        f=frame(at,period=period); f['book_ref']=str(at)
        f['book']['bid']=bid;f['book']['ask']=ask
        f['book']['bids']=[{'price':bid,'size':'50'}]
        f['book']['asks']=[{'price':ask,'size':'50'}]
        return self.session.observe(f)

    def test_split_is_cash_funded_and_two_sided(self):
        self.om.split(frame()['market'],'5','seed')
        self.assertEqual(self.om.account()['cash'],'20')
        self.assertEqual(set(self.om.account()['positions'].values()),{'5'})
        f=self.observed()
        d=book_targets(f,self.om.account(),[],{},self.om.policy)
        self.assertEqual(d['state'],'TWO_SIDED')
        self.assertEqual({x['side']:x['price'] for x in d['targets']},{'BUY':'0.48','SELL':'0.52'})
        self.assertTrue(self.om.consistent())

    def test_inventory_skew_symmetric_tick_and_submission_recheck(self):
        self.om.split(frame()['market'],'5','seed'); f=self.observed()
        a={**self.om.account(),'positions':{'a':'10','b':'5'}}
        p=prices(f,a,self.om.policy)
        self.assertEqual(str(p['BUY']),'0.47'); self.assertEqual(str(p['SELL']),'0.52')
        a['positions']={'a':'0','b':'5'}
        p=prices(f,a,self.om.policy);self.assertEqual(str(p['BUY']),'0.48');self.assertEqual(str(p['SELL']),'0.53')
        candidate=book_targets(f,self.om.account(),[],{},self.om.policy)['targets'][0]
        candidate['price']='0.49'
        self.assertIn('rotation_quote_edge_lost',submission_risk(candidate,f,self.om.account(),[],self.om.policy,AT))

    def test_fourth_quarter_reduce_even_without_verified_countdown(self):
        self.om.split(frame()['market'],'5','seed')
        f=frame(AT,period=4);f['sports']['remaining_s']=None;f['sports']['clock_evidence']=None
        f=self.session.observe(f)
        self.assertEqual(f['phase'],'REDUCE_ONLY')
        self.assertEqual(book_targets(f,self.om.account(),[],{},self.om.policy)['targets'],[])

    def test_shock_pauses_and_expired_cooldown_recovers_without_future_books(self):
        self.observed()
        f=self.observed(AT+1000,bid='.55',ask='.57')
        self.assertIn('rotation_price_shock_cooldown',book_targets(f,self.om.account(),[],{},self.om.policy)['reasons'])
        f=self.observed(AT+12000,bid='.55',ask='.57')
        self.assertNotIn('rotation_price_shock_cooldown',book_targets(f,self.om.account(),[],{},self.om.policy)['reasons'])
        from nba_recorder.inplay.rotation import decorate
        past=decorate(self.om.db,frame(AT),self.om.policy,AT)
        self.assertEqual(past['rotation']['pause_until_ms'],0)

    def test_stale_and_disconnected_no_quote(self):
        for change in ({'connected':False},{'book_received_ms':AT-10000}):
            f=self.session.observe({**frame(),**change})
            self.assertEqual(book_targets(f,self.om.account(),[],{},self.om.policy)['state'],'NO_QUOTE')

    def test_retain_safe_farther_order_but_not_edge_lost(self):
        self.om.split(frame()['market'],'5','seed');f=self.observed()
        self.session.quote(f,'one'); self.om.advance(AT+20)
        f=self.observed(AT+20,bid='.50',ask='.52')
        d=book_targets(f,self.om.account(),self.om.orders(active_only=True),{},self.om.policy)
        self.assertTrue(any(t['side']=='BUY' and t['action']=='RETAIN' for t in d['targets']))
        self.assertFalse(any(t['side']=='SELL' and t['action']=='RETAIN' for t in d['targets']))

    def test_dedup_restart_preserves_journal_cash_and_rotation(self):
        self.om.split(frame()['market'],'5','seed');e=events()[0]
        self.session.on_event(e);before=self.om.account();n=self.om.db.execute('select count(*) from ip_rotation').fetchone()[0]
        self.assertFalse(self.session.on_event(e));self.assertEqual(self.om.account(),before)
        self.store.close();self.store=SingleWriterStore(self.path)
        self.om=UnifiedPaperManager(self.store,policy(),AT)
        self.assertEqual(self.om.account(),before)
        self.om.advance(AT+1000); self.om.reconcile()
        self.assertTrue(self.om.consistent()); self.assertNotEqual(self.om.risk_state,'ACTIVE')
        self.assertEqual(self.om.db.execute('select count(*) from ip_rotation').fetchone()[0],n)
        self.assertEqual(self.om.real_submission_calls,0)

    def test_kill_cannot_submit(self):
        self.om.kill('test');self.session.on_event(events()[0]);self.assertEqual(self.om.orders(),[])

    def test_legacy_default_and_opt_in_validation(self):
        self.assertEqual(BookPolicy().quote_profile,'LEGACY')
        with self.assertRaises(ValueError):BookPolicy(quote_profile='INPLAY_ROTATION_V1')
        with self.assertRaises(ValueError):policy(rotation_jump='NaN')
        with self.assertRaises(ValueError):policy(rotation_cooldown_ms=0)

    def test_lazy_phase_import_with_previous_running_policy(self):
        from types import SimpleNamespace
        from nba_recorder.inplay.research_phase import permission
        legacy = SimpleNamespace(research_phase_age_ms=120000, close_remaining_s=60, reduce_remaining_s=180)
        f = {'market': {'market_id':'m','event_id':'e'}, 'sports': {
            'event_id':'e','identity_evidence':'test exact game identity','received_ms':AT,
            'status':'InProgress','live':True,'period':1}}
        self.assertEqual(permission(f, legacy, AT), ('ACTIVE_MM', []))
        f['sports'].update(period=4, remaining_s=120, clock_evidence='test verified clock')
        self.assertEqual(permission(f, legacy, AT), ('REDUCE_ONLY', []))
        self.assertEqual(permission(f, legacy, AT+120001)[0], 'UNKNOWN')
        f['sports']['event_id']='wrong'
        self.assertEqual(permission(f, legacy, AT)[0], 'UNKNOWN')


class CampaignTests(unittest.TestCase):
    def test_offline_lanes_models_and_reproducible_prefix(self):
        from nba_recorder.research.engine import freeze_recording
        from nba_recorder.research.rotation_study import lane, profile
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);output=root/'out';output.mkdir();sid='synthetic-test-only'
            (output/sid).mkdir();source=root/'source.sqlite'
            store=SingleWriterStore(source);om=UnifiedPaperManager(store,BookPolicy(research_phase=True),AT)
            session=IndexedHistoricalSession(om,'L2')
            stream=events()
            for e in stream:session.on_event(e)
            store.close()
            tape=root/'tape.sqlite';sample={'period':'SYNTHETIC','session_id':sid,'source':'SYNTHETIC_DATA','start_ms':AT,'end_ms':AT+44000,'market':frame()['market'],'source_input_sha256':'fixture'}
            sample['tape_id']=freeze_recording(source,tape,sample);sample['tape']=str(tape)
            values=[]; campaign_rows=[]
            for model in ('L0','L1','L2'):
                row=lane(root,output,sample,model,asdict(profile()));campaign_rows.append(row)
                r=json.loads((output/row['result']).read_text());values.append(r['fill_events'])
                self.assertEqual(r['network_attempts'],0);self.assertTrue(r['shutdown_ledger_consistent'])
                self.assertEqual(r['initial_split_shares'],'5')
                orders=json.loads((output/sid/model/'orders.json').read_text())
                self.assertTrue(all(o.get('purpose')=='RISK_EXIT' for o in orders if o['created_ms']>=AT+20000))
            self.assertGreater(len(set(values)),1)
            from nba_recorder.research.rotation_audit import validate
            (output/'manifest.json').write_text(json.dumps({'samples':[sample],'excluded':[]}))
            (output/'summary.json').write_text(json.dumps({'rows':campaign_rows,'errors':[]}))
            validation=validate(output)
            self.assertEqual(validation['status'],'PASS');self.assertEqual(validation['lanes'],3)
            import csv
            with (output/'metrics.csv').open(encoding='utf-8-sig') as fp:
                for row in csv.DictReader(fp):
                    if row['paper_resting_time_fraction']:self.assertTrue(0<=float(row['paper_resting_time_fraction'])<=1)
            import sqlite3
            from contextlib import closing
            with self.assertRaises(sqlite3.IntegrityError):
                with closing(sqlite3.connect(output/sid/'L0/paper.sqlite')) as db, db:db.execute("UPDATE pe_control SET mode='NON_PAPER_TEST_ONLY'")
            self.assertEqual(validate(output)['status'],'PASS')
            second=root/'second';(second/sid).mkdir(parents=True)
            row=lane(root,second,sample,'L1',asdict(profile()))
            self.assertEqual((second/sid/'L1/orders.json').read_bytes(),(output/sid/'L1/orders.json').read_bytes())
            self.assertEqual((second/sid/'L1/fills.json').read_bytes(),(output/sid/'L1/fills.json').read_bytes())


if __name__=='__main__':unittest.main()
