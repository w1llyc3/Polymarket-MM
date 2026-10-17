import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from nba_recorder.console import plans
from nba_recorder.console.insights import monitor, overlap_ms
from nba_recorder.console.history import review, save_report, view
from nba_recorder.console.research import analyze
from nba_recorder.console.service import ConsoleService
from nba_recorder.inplay.demo import AT,frame
from nba_recorder.inplay.runner import PaperRunner
from nba_recorder.fill_management.ledger import empty
from nba_recorder.inplay.policy import BookPolicy
from tests import test_next_iteration as baseline


class PlanTests(unittest.TestCase):
    def test_versions_are_immutable_and_cannot_carry_live_or_wallet_fields(self):
        with tempfile.TemporaryDirectory() as root:
            a=plans.save(root,'小额',{'quote_distance':'.03','max_order_lifetime_ms':90000})
            original=plans.load(root,a['id'],1)
            b=plans.save(root,'小额新版本',{'quote_distance':'.04'},a['id'],1)
            self.assertEqual(b['version'],2);self.assertEqual(plans.load(root,a['id'],1),original)
            with self.assertRaises(ValueError):plans.save(root,'旧版本覆盖',{},a['id'],1)
            for field in ('live','private_key','initial_cash','research_phase'):
                with self.assertRaises(ValueError):plans.save(root,'禁止字段',{field:True})
            with self.assertRaises(ValueError):plans.validate({'order_size':'20','max_order_shares':'5'})
            with self.assertRaises(ValueError):plans.load(root,'../wallet',1)

    def test_selected_plan_and_budget_bound_at_explicit_start(self):
        async def run(root):
            s=ConsoleService(root);p=plans.save(s.data,'跨场方案',{'quote_distance':'.04'})
            captured=[]
            async def session(m):captured.append((s.paper_budget,s.research_overrides,s.bound_plan))
            s.session=session
            s.handle({'session_id':None,'expected_version':s.version,'action':'start','mode':'SYNTHETIC_DEMO','confirmed':True,'paper_budget':'25','plan_id':p['id'],'plan_version':1})
            await s.task
            self.assertEqual(captured[0][0],'25');self.assertEqual(captured[0][1]['quote_distance'],'.04')
        with tempfile.TemporaryDirectory() as root:asyncio.run(run(root))


class MetricsTests(unittest.TestCase):
    def test_time_denominator_excludes_operator_pause_and_never_counts_frames(self):
        f=frame(0);o={'id':'o','asset_id':'a','state':'OPEN'}
        def event(t,kind,state='ACTIVE',order=None,detail=None):return {'seq':t+1,'time_ms':t,'kind':kind,'risk_state':state,'order':order,'detail':detail or {},'positions':{'a':'2'}}
        events=[event(0,'INITIALIZED'),event(100,'EXECUTION_REPORT',order=o,detail={'authority':{'state':'OPEN'}}),
                event(400,'CANCEL_REQUEST','PAUSED',o,{'reason':'OPERATOR'}),event(600,'EXECUTION_REPORT','PAUSED',{**o,'state':'CANCELLED'},{'authority':{'state':'CANCELLED'}})]
        prices={'a':[{'time_ms':0,'mid':'.5','bid':'.49','ask':'.51','depth_shares':'10'}]}
        metrics=monitor(prices,[],empty(),events,[{'targets':[{}],'frame':f}],1000,BookPolicy())
        self.assertEqual(metrics['availability']['a']['eligible_decision_ms'],400)
        self.assertEqual(metrics['availability']['a']['accepted_eligible_ms'],300)
        self.assertEqual(metrics['cancel_reasons']['OPERATOR'],1)
        self.assertEqual(overlap_ms([(0,10),(0,10)],[(5,15)]),5)

    def test_recent_growth_uses_pre_window_baseline_and_stale_spread_is_unknown(self):
        events=[{'seq':1,'time_ms':0,'kind':'INITIALIZED','risk_state':'ACTIVE','order':None,'detail':{},'positions':{'a':'2'}},
                {'seq':2,'time_ms':200000,'kind':'EXECUTION_REPORT','risk_state':'ACTIVE','order':None,'detail':{},'positions':{'a':'5'}}]
        ps={'a':[{'time_ms':200000,'mid':'.5','bid':'.49','ask':'.51','depth_shares':'10','valid_until_ms':203000}]}
        result=monitor(ps,[],empty(),events,[],210000,BookPolicy())
        self.assertEqual(result['assets']['a']['recent_inventory_growth_shares'],'3')
        self.assertIsNone(result['assets']['a']['current_spread_cents'])


class CompletionTests(unittest.TestCase):
    setUp=baseline.ResearchTests.setUp
    tearDown=baseline.ResearchTests.tearDown
    feed=baseline.ResearchTests.feed

    def test_later_parameter_version_does_not_change_old_analysis(self):
        self.feed();before=analyze(self.path,AT)
        self.om.advance(AT+1);self.om.change_research_parameters({'quote_distance':'.03'})
        self.assertEqual(before,analyze(self.path,AT))

    def test_review_keeps_original_notes_and_excludes_future_inputs(self):
        self.feed();r=save_report(self.tmp.name,self.sid,'Q4_MANUAL',AT,'等待明确条件，不预测赢家')
        file=Path(self.tmp.name)/(self.sid+'-report-'+r['report_id']+'.json');raw=file.read_bytes()
        self.feed(AT+1000);comparison=review(self.tmp.name,self.sid,r['report_id'],AT)
        self.assertEqual(comparison['subsequent_observed_paths'],{})
        later=review(self.tmp.name,self.sid,r['report_id'])
        self.assertTrue(later['subsequent_observed_paths']);self.assertEqual(file.read_bytes(),raw)
        self.assertEqual(later['original_sha256'],hashlib.sha256(raw).hexdigest())
        self.assertEqual(later['original']['note'],r['note'])

    def test_history_exposes_residual_inventory_without_creating_capital(self):
        self.feed();count=self.om.db.execute("SELECT count(*) FROM l6_events WHERE kind='CAPITAL'").fetchone()[0]
        v=view(self.tmp.name,self.sid)
        self.assertIn('positions',v['state']['account']);self.assertFalse(v['ended'])
        self.assertEqual(count,self.om.db.execute("SELECT count(*) FROM l6_events WHERE kind='CAPITAL'").fetchone()[0])

    def test_manual_q3_report_note_is_not_phase_or_order_authority(self):
        self.feed();orders=self.om.orders();phase=self.om.phases.get('m')
        report=save_report(self.tmp.name,self.sid,'Q4_MANUAL',AT,'合成备注',True)
        self.assertTrue(report['manual_q3_ended']['confirmed'])
        self.assertEqual(self.om.orders(),orders);self.assertEqual(self.om.phases.get('m'),phase)
        with self.assertRaises(ValueError):analyze(self.path,-1)

    def test_direction_preparation_reconciles_and_never_reopens_orders(self):
        fs=self.feed();self.om.advance(AT+10)
        s=ConsoleService(self.tmp.name);s.session_id=self.sid;s.lifecycle='RUNNING';s.mode='REAL_PAPER'
        s.runner=PaperRunner(self.om,'L1');s.runner.latest={f['asset_id']:f for f in fs};s.bound_market=fs[0]['market'];s.task=SimpleNamespace(done=lambda:False)
        s.publish=lambda **kwargs:None
        s.handle({'session_id':self.sid,'expected_version':s.version,'action':'prepare_direction','confirmed':True})
        self.om.advance(AT+40);before=len(self.om.orders());asyncio.run(s.pump())
        self.assertTrue(s.runner.paper_stopped);self.assertEqual(self.om.risk_state,'HALTED');self.assertEqual(len(self.om.orders()),before)
        self.assertEqual(analyze(self.path)['direction_preparation']['status'],'READY_FOR_MANUAL_DIRECTION_RESEARCH')
        self.assertEqual(self.om.real_submission_calls,0)

    def test_mutating_commands_cannot_run_while_stopping(self):
        s=ConsoleService(self.tmp.name);s.session_id=self.sid;s.lifecycle='STOPPING';s.mode='REAL_PAPER'
        s.runner=PaperRunner(self.om,'L1');s.task=SimpleNamespace(done=lambda:False)
        with self.assertRaises(ValueError):s.handle({'session_id':self.sid,'expected_version':s.version,'action':'update_plan','confirmed':True,'research_params':{'quote_distance':'.03'}})

    def test_directional_risk_does_not_offset_pending_complement_sells(self):
        from dataclasses import replace
        from nba_recorder.inplay.reference import submission_risk
        f=frame(AT);f['phase']='ACTIVE_MM';policy=replace(self.policy,max_one_sided_shares='5')
        account={'cash':'100','positions':{'a':'5','b':'5'},'events':{'a':'e','b':'e'}}
        pending={'id':'other','asset_id':'b','event_id':'e','side':'SELL','state':'CANCEL_PENDING','price':'.52','size':'5','filled':'0'}
        buy={'asset_id':'a','market_id':f['market_id'],'event_id':f['market']['event_id'],'side':'BUY','price':'.48','size':'5','maker_only':True,'expires_ms':AT+1000}
        self.assertIn('directional_inventory_soft_limit',submission_risk(buy,f,account,[pending],policy,AT))
        sell={**buy,'side':'SELL','price':'.52'}
        self.assertNotIn('directional_inventory_soft_limit',submission_risk(sell,f,account,[pending],policy,AT))

    def test_history_includes_cleanup_cancel_confirmation_after_last_book(self):
        self.feed();self.om.advance(AT+10);self.om.kill('operator_stop');self.om.advance(AT+50)
        last=self.om.db.execute('SELECT max(time_ms) FROM pe_journal').fetchone()[0]
        history=view(self.tmp.name,self.sid)
        self.assertEqual(history['chart']['end_ms'],last);self.assertGreater(last,AT)
