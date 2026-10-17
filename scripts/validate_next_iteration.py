"""Three-hour *virtual*, sparse synthetic replay. No live endpoints or orders."""
import json
import time
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from nba_recorder.inplay.demo import AT, frame
from nba_recorder.inplay.policy import BookPolicy
from nba_recorder.inplay.manager import UnifiedPaperManager
from nba_recorder.inplay.session import BookSession
from nba_recorder.inplay.store import SingleWriterStore
from nba_recorder.console.history import save_report
from nba_recorder.console.research import analyze


def main():
    root=Path('data/next-long');root.mkdir(parents=True,exist_ok=True)
    import uuid
    sid=uuid.uuid4().hex;path=root/(sid+'.sqlite');store=SingleWriterStore(path)
    started=time.perf_counter();cpu=time.process_time();peak=0
    try:
        om=UnifiedPaperManager(store,BookPolicy(research_phase=True),AT);session=BookSession(om,'L1')
        with om.db:om.db.execute("INSERT INTO ip_session_config VALUES('console_origin',?)",(json.dumps({'data_source':'SYNTHETIC_DATA','execution_source':'PAPER_EXECUTION'}),))
        om.split(frame(AT)['market'],'5','explicit-collateral-funded-synthetic-split')
        for i in range(721):
            at=AT+i*15000;period=min(4,1+i//180)
            fs=[frame(at,a,period,600) for a in ('a','b')]
            for f in fs:
                f['market'].update(rules_verified=True,research_authorized=True)
                f['sports'].update(observed_ms=None,paused=None)
            trades=[]
            if i in (4,12,20):trades=[{'event_id':f'long-print-{i}','asset_id':'a','condition_id':fs[0]['market']['condition_id'],'exchange_ms':at,'side':'SELL','price':'.47','size':'2'}]
            session.on_event({'id':f'long-{i}','time_ms':at,'frames':fs,'trades':trades})
            if i==100:om.pause('synthetic_operator_pause')
            if i==102:
                om.advance(at+100);om.reconcile();assert om.resume()
            peak=max(peak,path.stat().st_size)
        om.kill('synthetic_run_limit_not_real_game_end');om.advance(om.now+1000);om.reconcile()
        assert om.consistent();assert om.real_submission_calls==0
        t=time.perf_counter();result=analyze(path);analysis_seconds=time.perf_counter()-t
        report=save_report(root,sid,'POST_SESSION')
        result={'source':'SYNTHETIC_DATA','virtual_duration_seconds':10800,'wall_seconds':time.perf_counter()-started,
                'cpu_seconds':time.process_time()-cpu,'snapshot_cadence_seconds':15,
                'continuous_freshness_proven':False,'watchdog_wall_scheduling_proven':False,
                'database_bytes':path.stat().st_size,'wal_bytes':Path(str(path)+'-wal').stat().st_size if Path(str(path)+'-wal').exists() else 0,
                'analysis_seconds':analysis_seconds,'ledger_consistent':om.consistent(),'real_submission_calls':0,
                'paper_orders':len(om.orders()),'fills':result.get('sample_size'),'report_id':report['report_id'],
                'limitations':['加速稀疏合成流，不是整场真实比赛','未测浏览器4小时内存/首载和实际网络积压','没有伪造实盘性能']}
        out=Path('evidence/next-iteration');out.mkdir(parents=True,exist_ok=True)
        (out/'long-replay.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(result,ensure_ascii=False))
    finally:store.close()


if __name__=='__main__':main()
