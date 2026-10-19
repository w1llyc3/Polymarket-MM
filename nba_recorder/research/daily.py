"""Immutable post-capture audits. Reads a SQLite backup, never constructs an OMS."""
import argparse
import hashlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path

from ..fair_value.maths import decimal
from ..fair_value.store import digest, read_connection
from ..console.history import resolve
from ..console.research import analyze, markouts
from ..fill_management.ledger import Ledger, balance
from ..inplay.observations import price_point
from ..inplay.policy import BookPolicy
from ..unattended import bounds


def publish(path,value,replace=False):
    """Atomic publication; immutable versions never expose partially written JSON."""
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=path.parent,delete=False) as fp:
        temp=Path(fp.name)
        fp.write(value if isinstance(value,str) else json.dumps(value,ensure_ascii=False,indent=2))
        fp.flush();os.fsync(fp.fileno())
    try:
        if replace:os.replace(temp,path)
        else:
            try:os.link(temp,path)
            except FileExistsError:pass
    finally:
        if temp.exists():temp.unlink()


def _audit(path, sid):
    db=read_connection(path)
    try:
        config=dict(db.execute('SELECT id,payload FROM ip_session_config'))
        origin=json.loads(config.get('console_origin','{}'))
        control=db.execute('SELECT config,account FROM pe_control WHERE id=1').fetchone()
        policy=BookPolicy(**json.loads(control['config']));account=json.loads(control['account'])
        prices={};unique={};markets={};frames=watchdogs=0;start=end=None
        input_hash=hashlib.sha256();input_cursor=0
        for row in db.execute('SELECT rowid,time_ms,payload FROM ip_inputs ORDER BY time_ms,rowid'):
            input_cursor=max(input_cursor,row['rowid']);e=json.loads(row['payload']);at=row['time_ms']
            input_hash.update((str(at)+'\t'+row['payload']+'\n').encode())
            if start is None:start=at
            end=at;watchdogs+=e.get('kind')=='WATCHDOG'
            for f in e['frames']:
                frames+=1;a=f['asset_id'];markets[f['market_id']]=f['market']
                prices.setdefault(a,[]).append(price_point(f,policy))
                if e.get('kind')!='WATCHDOG':unique.setdefault(a,set()).add((f.get('book_ref'),f.get('book_received_ms')))
        coverage={}
        for a,rows in prices.items():
            valid_ms=0;largest_gap=0;last_valid_end=start
            for i,p in enumerate(rows):
                stop=min(end,rows[i+1]['time_ms'] if i+1<len(rows) else end,p['valid_until_ms'])
                if p['mid'] is None or stop<=p['time_ms']:continue
                largest_gap=max(largest_gap,p['time_ms']-last_valid_end)
                valid_ms+=stop-p['time_ms'];last_valid_end=stop
            largest_gap=max(largest_gap,end-last_valid_end)
            coverage[a]={'valid_ms':valid_ms,'window_ms':end-start,'ratio':str(decimal(valid_ms)/(end-start)) if end>start else None,
                'largest_uncovered_ms':largest_gap,'unique_book_observations':len(unique.get(a,set()))}
        ledger_checks=[];ledger_state=None
        for row in db.execute("SELECT run_id,lane FROM l6_accounts WHERE lane='UNIFIED_PAPER'"):
            ledger=Ledger(db,row['run_id'],row['lane']);state=ledger.state();ledger_state=state
            replay_ok=ledger.rebuild()==state
            keys=set(account.get('positions',{}))|{lot['asset_id'] for lot in state['lots']}
            match=decimal(state['cash'])==decimal(account['cash']) and all(balance(state,a)==decimal(account['positions'].get(a,'0')) for a in keys)
            ledger_checks.append({'event_replay_equals_cache':replay_ok,'cash_positions_equal_oms':match})
        unresolved=db.execute("SELECT count(*) FROM pe_orders WHERE state NOT IN ('CANCELLED','FILLED','REJECTED') AND NOT (state='RECONCILED' AND json_extract(payload,'$.confirmed_state') IN ('CANCELLED','FILLED','REJECTED'))").fetchone()[0]
        finished=db.execute("SELECT count(*) FROM pe_journal WHERE kind='CONSOLE_SESSION_FINISHED'").fetchone()[0]>0
        journal_cursor=db.execute('SELECT coalesce(max(seq),0) FROM pe_journal').fetchone()[0]
        ledger_cursor=db.execute('SELECT coalesce(max(seq),0) FROM l6_events').fetchone()[0]
        completion=None
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='ip_saved_report'").fetchone():
            row=db.execute('SELECT payload FROM ip_saved_report ORDER BY id DESC LIMIT 1').fetchone()
            if row:completion=json.loads(row[0]).get('recording_coverage')
        fills=[]
        for row in db.execute("SELECT id,time_ms,payload FROM l6_events WHERE kind='FILL' ORDER BY seq"):
            fill=json.loads(row['payload'])
            if fill.get('purpose')=='SIMULATED_COMPLETE_SET_SPLIT':continue
            fills.append({**fill,'id':row['id'],'time_ms':row['time_ms']})
        tips=[m['game_start_ms'] for m in markets.values() if type(m.get('game_start_ms')) is int]
        reasons=[]
        if not finished:reasons.append('SESSION_NOT_FINISHED')
        if not frames:reasons.append('NO_BOOK_FRAMES')
        expected={o['asset_id'] for m in markets.values() for o in m.get('outcomes',[])}
        if expected-set(prices):reasons.append('MISSING_OUTCOME_BOOK')
        if not ledger_checks or not all(c['event_replay_equals_cache'] and c['cash_positions_equal_oms'] for c in ledger_checks):reasons.append('LEDGER_MISMATCH_OR_MISSING')
        if unresolved:reasons.append('UNRESOLVED_OR_ACTIVE_ORDERS')
        if any(c['ratio'] is None or decimal(c['ratio'])<1 for c in coverage.values()):reasons.append('DATA_GAPS')
        if origin.get('data_source')!='REAL_OBSERVED':reasons.append('NOT_REAL_OBSERVED')
        if tips and end is not None and end<min(tips):reasons.append('PRE_GAME_ONLY')
        if tips and start is not None and start>min(tips)-3600000:reasons.append('MISSED_PRE_GAME_HOUR')
        if not completion or completion.get('stop_reason')!='OBSERVED_GAME_END_PLUS_TAIL':reasons.append('END_AND_TAIL_NOT_OBSERVED')
        cutoff=max((v for v in (end,db.execute('SELECT max(time_ms) FROM pe_journal').fetchone()[0]) if v is not None),default=None)
        audit={'version':'postcapture-v1','session_id':sid,'origin':origin,'input_sha256':input_hash.hexdigest(),'input_cursor':input_cursor,
            'journal_cursor':journal_cursor,'ledger_cursor':ledger_cursor,'as_of_ms':cutoff,'start_ms':start,'last_input_ms':end,
            'markets':list(markets.values()),'input_asset_frames':frames,'watchdog_callbacks':watchdogs,
            'coverage':coverage,'ledger_checks':ledger_checks,'unresolved_or_active_orders':unresolved,
            'session_finished':finished,'completion':completion,'reasons':reasons,'full_game_verified':False,
            'data_status':'NO_DATA' if not frames else 'BOUNDED_WITH_GAPS' if 'DATA_GAPS' in reasons else 'BOUNDED_OBSERVATIONS',
            'paper_status':'REVIEW_REQUIRED' if any(r in reasons for r in ('SESSION_NOT_FINISHED','LEDGER_MISMATCH_OR_MISSING','UNRESOLVED_OR_ACTIVE_ORDERS')) else 'LOCAL_LEDGER_VERIFIED_NOT_STRATEGY_ACCEPTANCE',
            'markouts':{a:markouts([f for f in fills if f['asset_id']==a],rows,cutoff,(10,30,60,300)) for a,rows in prices.items()} if cutoff is not None else {},
            'ledger_cash':ledger_state['cash'] if ledger_state else None,'real_submission_calls':0,'live_go':False,
            'limits':['Coverage is fresh observed-book time within recorded window, not proof of full game',
                'Watchdog reuse does not count as unique book observation','Paper ledger verification is not external account reconciliation',
                'All fills and PnL are simulated; fees/rewards and actual execution quality remain unverified']}
        return audit
    finally:db.close()


def review_session(root,sid):
    root=Path(root);source=resolve(root/'data/console',sid)
    # Backup gives audit, metrics and immutable report the exact same input view.
    with tempfile.TemporaryDirectory(prefix='nba-postcapture-') as temp:
        frozen=Path(temp)/'snapshot.sqlite';reader=read_connection(source)
        target=sqlite3.connect(frozen)
        try:reader.backup(target)
        finally:target.close();reader.close()
        audit=_audit(frozen,sid)
        with frozen.open('rb') as fp:audit['snapshot_sha256']=hashlib.file_digest(fp,'sha256').hexdigest()
        identity=digest(audit);folder=root/'data/postcapture'/sid;folder.mkdir(parents=True,exist_ok=True)
        target=folder/(identity+'.json')
        if target.exists():return {'id':identity,'session_id':sid,'status':'EXISTS','path':str(target.relative_to(root)),'audit':audit}
        analysis=analyze(frozen,audit['as_of_ms']) if audit['session_finished'] else None
        value={'id':identity,'session_id':sid,'audit':audit,'analysis':analysis,'not_real_profit':True,'real_submission_calls':0,'live_go':False}
        text='# NBA post-capture review\n\nPaper simulation only; not real profit.\n\n'+json.dumps(value,ensure_ascii=False,indent=2)+'\n'
        publish(target.with_suffix('.md'),text)
        publish(target,value)
        return {'id':identity,'session_id':sid,'status':'CREATED','path':str(target.relative_to(root)),'audit':audit}


def review_day(root,day):
    root=Path(root);bounds(day)
    state_path=root/'data/unattended'/day/'state.json'
    state=json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {'games':{}}
    sessions=[]
    for market_id,entry in sorted(state['games'].items()):
        item={'market_id':market_id,'session_id':entry['session_id'],'recording_status':entry['status']}
        if entry['status'] in {'FINISHED','FAILED_NEEDS_REVIEW'}:
            try:
                report=review_session(root,entry['session_id'])
                item['review']={k:report[k] for k in ('id','session_id','path')}
                item['audit_summary']={k:report['audit'][k] for k in ('data_status','paper_status','reasons','coverage','ledger_checks','full_game_verified','as_of_ms')}
            except Exception as error:item['error']=type(error).__name__
        else:item['review_status']='WAITING_FOR_RECORDING_END'
        sessions.append(item)
    value={'day':day,'sessions':sessions,'discovery_status':state.get('discovery_status'),'schedule_only':state.get('schedule_only'),
        'status':'REVIEW_FAILED' if any(s.get('error') for s in sessions) else 'NO_RECORDED_SESSIONS' if not sessions else 'REVIEWS_AVAILABLE' if any(s.get('review') for s in sessions) else 'WAITING',
        'real_submission_calls':0,'live_go':False,'all_games_coverage_verified':False}
    destination=root/'data/postcapture/days'/day;destination.mkdir(parents=True,exist_ok=True)
    identity=digest(value);publish(destination/(identity+'.json'),value)
    publish(destination/'latest.json',{'id':identity,**value},replace=True)
    return value


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--date');parser.add_argument('--session')
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[2]);args=parser.parse_args()
    if bool(args.date)==bool(args.session):parser.error('Choose --date or --session')
    result=review_day(args.root,args.date) if args.date else review_session(args.root,args.session)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    if result.get('status')=='REVIEW_FAILED':raise SystemExit(1)


if __name__=='__main__':main()
