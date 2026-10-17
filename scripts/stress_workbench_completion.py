"""Dense four-hour virtual Paper replay plus a separate short wall-clock watchdog drill."""
import asyncio
import json
import sys
import time
import uuid
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from nba_recorder.api import utc_ms
from nba_recorder.inplay.demo import AT,frame
from nba_recorder.inplay.policy import BookPolicy
from nba_recorder.inplay.manager import UnifiedPaperManager
from nba_recorder.inplay.session import BookSession
from nba_recorder.inplay.runner import PaperRunner
from nba_recorder.inplay.store import SingleWriterStore
from nba_recorder.console.research import analyze
from nba_recorder.console.chart import query
from nba_recorder.console.history import save_report

OUT=Path('evidence/workbench-completion');OUT.mkdir(parents=True,exist_ok=True)
ROOT=Path('data/completion-stress');ROOT.mkdir(parents=True,exist_ok=True)


async def wall_drill():
    path=ROOT/(uuid.uuid4().hex+'.sqlite');store=SingleWriterStore(path)
    try:
        om=UnifiedPaperManager(store,BookPolicy(research_phase=True),utc_ms());runner=PaperRunner(om,'L1')
        om.split(frame(om.now)['market'],'5','wall-drill-explicit-collateral')
        connected=True;runner.connection_guard=lambda:connected
        ticks=[];original=runner.tick
        def tick(*args):ticks.append(time.monotonic());return original(*args)
        runner.tick=tick;task=asyncio.create_task(runner.watchdog());runner.watchdog_task=task
        started=time.monotonic();next_feed=0;paused=False;resumed=False;paused_submissions=None
        while time.monotonic()-started<10:
            elapsed=time.monotonic()-started;connected=not 2<=elapsed<3.5
            if elapsed>=4 and not paused:
                runner.quotes_enabled=False;om.pause('operator_wall_drill');paused=True
                paused_submissions=om.db.execute("SELECT count(*) FROM pe_journal WHERE kind='SUBMIT_REQUEST'").fetchone()[0]
            if connected and elapsed>=next_feed:
                at=utc_ms();runner.feed([frame(at,a) for a in ('a','b')],provenance='SYNTHETIC_WALL_CLOCK_DRILL');next_feed=elapsed+.4
            if elapsed>=7 and not resumed:
                assert om.db.execute("SELECT count(*) FROM pe_journal WHERE kind='SUBMIT_REQUEST'").fetchone()[0]==paused_submissions
                om.reconcile();assert om.resume();runner.quotes_enabled=True;resumed=True
            await asyncio.sleep(.02)
        runner.stopping.set();await task
        om.kill('wall_drill_complete');om.advance(om.now+200);assert om.reconcile();assert om.consistent()
        gaps=[1000*(b-a) for a,b in zip(ticks,ticks[1:])]
        return {'source':'SYNTHETIC_WALL_CLOCK_DRILL','wall_seconds':time.monotonic()-started,'ticks':len(ticks),
                'max_tick_gap_ms':max(gaps,default=None),'pause_kept_submissions_constant':True,'explicit_resume':resumed,
                'ledger_consistent':om.consistent(),'real_submission_calls':om.real_submission_calls,'not_four_hour_wall_run':True}
    finally:store.close()


def dense():
    import subprocess
    commit=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
    dirty=bool(subprocess.check_output(['git','status','--porcelain'],text=True).strip())
    sid=uuid.uuid4().hex;path=ROOT/(sid+'.sqlite');store=SingleWriterStore(path)
    start=time.perf_counter();cpu=time.process_time();query_seconds=[]
    try:
        om=UnifiedPaperManager(store,BookPolicy(research_phase=True),AT);session=BookSession(om,'L1')
        with om.db:om.db.execute("INSERT INTO ip_session_config VALUES('console_origin',?)",(json.dumps({'data_source':'SYNTHETIC_DATA','execution_source':'PAPER_EXECUTION'}),))
        om.split(frame(AT)['market'],'5','dense-explicit-collateral')
        for i in range(14401):
            at=AT+i*1000;fs=[frame(at,a,period=min(4,1+i//3600),remaining=600) for a in ('a','b')]
            session.on_event({'id':'dense-'+str(i),'time_ms':at,'frames':fs,'trades':[]})
            if i and i%3600==0:print('virtual hours',i//3600,flush=True)
        om.kill('synthetic_hard_limit_not_game_end');om.advance(om.now+200);assert om.reconcile();assert om.consistent()
        t=time.perf_counter();r=analyze(path);analysis_s=time.perf_counter()-t
        t=time.perf_counter();save_report(ROOT,sid,'POST_SESSION');report_s=time.perf_counter()-t
        cursors={};pages=0;more=True
        while more:
            t=time.perf_counter();p=query(path,'a',AT,om.now,cursors);query_seconds.append(time.perf_counter()-t)
            more=p['has_more'];cursors=p['cursors'];pages+=1
        return {'source':'SYNTHETIC_DATA','code_commit':commit,'working_tree_dirty':dirty,'virtual_hours':4,'snapshot_cadence_ms':1000,
                'wall_seconds':time.perf_counter()-start,'cpu_seconds':time.process_time()-cpu,'database_bytes':path.stat().st_size,
                'wal_bytes':Path(str(path)+'-wal').stat().st_size,'paper_order_count':len(om.orders()),'ledger_consistent':om.consistent(),
                'real_submission_calls':om.real_submission_calls,'analysis_seconds':analysis_s,'report_seconds':report_s,
                'chart_server_pages':pages,'chart_first_page_server_seconds':query_seconds[0],'chart_max_page_server_seconds':max(query_seconds),
                'chart_has_more_at_end':more,'valid_record_window_coverage':r['data_coverage'],
                'browser_render_memory_not_measured':True,'real_game_coverage_not_verified':True,'session_id':sid}
    finally:store.close()


if __name__=='__main__':
    wall=asyncio.run(wall_drill());(OUT/'wall-drill.json').write_text(json.dumps(wall,ensure_ascii=False,indent=2),encoding='utf-8')
    result=dense();(OUT/'dense-four-hours.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))
