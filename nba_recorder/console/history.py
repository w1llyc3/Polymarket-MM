"""Local registered session IDs; read-only projection, immutable report versions."""
import json
import re
import uuid
import hashlib
from datetime import datetime,timezone
from pathlib import Path
from ..fair_value.store import read_connection
from .research import analyze


def resolve(root, sid):
    if not isinstance(sid,str) or not re.fullmatch('[0-9a-f]{32}',sid):raise ValueError('invalid session ID')
    root=Path(root).resolve();path=root/(sid+'.sqlite')
    if path.is_symlink() or not path.is_file() or path.resolve().parent!=root:raise ValueError('unregistered session')
    db=read_connection(path)
    try:
        row=db.execute("SELECT payload FROM ip_session_config WHERE id='console_origin'").fetchone()
        if not row:raise ValueError('not a console session')
    finally:db.close()
    return path


def view(root,sid):
    path=resolve(root,sid);db=read_connection(path)
    try:
        db.execute('BEGIN')
        first=db.execute('SELECT time_ms,payload FROM ip_inputs ORDER BY time_ms,rowid LIMIT 1').fetchone()
        last=db.execute('SELECT time_ms,payload FROM ip_inputs ORDER BY time_ms DESC,rowid DESC LIMIT 1').fetchone()
        origin=json.loads(db.execute("SELECT payload FROM ip_session_config WHERE id='console_origin'").fetchone()[0])
        if db.execute("SELECT 1 FROM pe_journal WHERE kind='EXECUTION_SEGMENT_START' LIMIT 1").fetchone():origin['execution_source']='OBSERVATION_THEN_PAPER'
        frames=json.loads(last[1])['frames'] if last else []
        control=db.execute('SELECT state,account FROM pe_control WHERE id=1').fetchone()
        account=json.loads(control[1]);completion=None
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='ip_saved_report'").fetchone():
            saved=db.execute('SELECT payload FROM ip_saved_report ORDER BY id DESC LIMIT 1').fetchone()
            if saved:
                report=json.loads(saved[0]);completion=report.get('recording_coverage') or {'reason':'SYNTHETIC_END' if origin.get('data_source')=='SYNTHETIC_DATA' else 'RESEARCH_ENDED','full_game_verified':False}
        uncertain=db.execute("SELECT count(*) FROM pe_orders WHERE state IN ('UNKNOWN','TIMEOUT','SUBMITTING','CANCEL_PENDING') OR (state='RECONCILED' AND coalesce(json_extract(payload,'$.confirmed_state'),'') NOT IN ('CANCELLED','FILLED','REJECTED'))").fetchone()[0]
        journal_end=db.execute('SELECT max(time_ms) FROM pe_journal').fetchone()[0]
        return {'session_id':sid,'lifecycle':'HISTORICAL_READ_ONLY','mode':'SYNTHETIC_DEMO' if origin.get('data_source')=='SYNTHETIC_DATA' else 'OBSERVATION' if origin.get('execution_source')=='OBSERVATION_ONLY' else 'REAL_PAPER',
                'chart':{'start_ms':first[0] if first else None,'end_ms':max(last[0],journal_end or last[0]) if last else None},
                'state':{'books':[{'asset_id':f['asset_id'],'label':next(o['label'] for o in f['market']['outcomes'] if o['asset_id']==f['asset_id']),'book':f.get('book')} for f in frames],
                         'bound_market':frames[0]['market'] if frames else {},'risk_state':control[0],
                         'account':{'positions':account.get('positions',{}),'cash':account.get('cash')},'unresolved_order_count':uncertain},
                'completion':completion,'ended':completion is not None,'reconciliation_state':'UNRESOLVED' if uncertain else 'NO_UNCERTAIN_LOCAL_ORDERS_NOT_INDEPENDENT_PROOF',
                'read_only':True,'server_ms':0,'origin':origin,'file_reference':path.name}
    finally:db.close()


def index(root):
    result=[]
    for path in sorted(Path(root).glob('*.sqlite'),key=lambda p:p.stat().st_mtime,reverse=True):
        if not re.fullmatch('[0-9a-f]{32}',path.stem):continue
        try:result.append(view(root,path.stem))
        except (ValueError,OSError, __import__('sqlite3').Error):continue
        if len(result)>=200:break
    return result


def save_report(root,sid,kind,as_of=None,note='',manual_q3_ended=False):
    if kind not in {'Q4_MANUAL','Q4_AUTO','POST_SESSION','FINAL_UPDATE'}:raise ValueError('report type')
    if not isinstance(note,str) or len(note)>2000:raise ValueError('bounded note')
    if type(manual_q3_ended) is not bool:raise ValueError('manual observation must be boolean')
    path=resolve(root,sid);result=analyze(path,as_of)
    identity=uuid.uuid4().hex
    db=read_connection(path)
    try:origin=json.loads(db.execute("SELECT payload FROM ip_session_config WHERE id='console_origin'").fetchone()[0])
    finally:db.close()
    if result.get('execution_segments'):origin['execution_source']='OBSERVATION_THEN_PAPER'
    result.update(report_id=identity,session_id=sid,kind=kind,note=note,generated_at=datetime.now(timezone.utc).isoformat(),
                  manual_q3_ended={'confirmed':manual_q3_ended,'source':'MANUAL_REPORT_NOTE_NOT_ORDER_PERMISSION'},
                  note_provenance='entered at report generation; historical review notes are not evidence of contemporaneous knowledge',
                  report_basis='manual research request' if kind=='Q4_MANUAL' else kind,
                  source=origin,not_real_profit=True)
    target=Path(root)/(sid+'-report-'+identity+'.json')
    # Exclusive creation: late information can only create a new report.
    markdown='# NBA 研究报告 · '+kind+'\n\n生成 '+result['generated_at']+'；数据截至 '+str(result.get('as_of_ms'))+'；Paper模拟，不是实盘收益。\n\n'
    rows=[('当时备注',note),('来源/模型',{'source':result['source'],'fill_model':result.get('fill_model')}),
          ('覆盖/终止',{'recorded_window':result.get('data_coverage'),'completion':result.get('session_completion'),'full_game_coverage':'NOT_VERIFIED'}),
          ('市场结构/成交分布',{a:{k:v.get(k) for k in ('price_structure','flow','recent_distribution','previous_distribution','planned_liquidity')} for a,v in result.get('assets',{}).items()}),
          ('库存风险',{'A_minus_B':result.get('net_directional_shares'),'paired':result.get('paired_shares'),'lots':result.get('inventory_lots'),'reserves':result.get('account')}),
          ('做市/退出/系统',{'monitoring':result.get('monitoring'),'exits':result.get('exit_candidates')}),
          ('资金及奖励',{'ledger':result.get('valuation'),'income_stages':result.get('income_stages'),'attribution':'Markout与滑点诊断不再加减到PnL'}),
          ('候选及失效条件',result.get('candidate_plan','继续观察')),('样本和限制',{'samples':result.get('sample_size'),'limits':result.get('limitations')})]
    for title,value in rows:
        markdown+='## '+title+'\n\n'+(value if isinstance(value,str) else json.dumps(value,ensure_ascii=False,indent=2))+'\n\n'
    markdown+='完整输入游标和统计见同名JSON。报告不可覆盖；新增数据需生成新版本。\n'
    # Publish Markdown first, then the JSON index entry. Readers never see partial JSON.
    for destination,contents in ((target.with_suffix('.md'),markdown),(target,json.dumps(result,ensure_ascii=False,indent=2))):
        temporary=Path(root)/(uuid.uuid4().hex+'.tmp')
        try:
            temporary.write_text(contents,encoding='utf-8')
            __import__('os').link(temporary,destination)
        finally:temporary.unlink(missing_ok=True)
    return result


def review(root,sid,report_id,as_of=None):
    """A separate retrospective projection; never rewrites the original report."""
    path=resolve(root,sid)
    if not isinstance(report_id,str) or not re.fullmatch('[0-9a-f]{32}',report_id):raise ValueError('invalid report ID')
    file=Path(root)/(sid+'-report-'+report_id+'.json')
    if file.is_symlink():raise ValueError('report symlink')
    raw=file.read_bytes();original=json.loads(raw)
    if original['session_id']!=sid:raise ValueError('report session mismatch')
    evaluation=analyze(path,as_of)
    start,end=original.get('as_of_ms'),evaluation.get('as_of_ms')
    if start is None or end is None or end<start:raise ValueError('review cutoff precedes original report')
    db=read_connection(path);paths={}
    try:
        from ..inplay.reference import book_data_risk
        from ..inplay.policy import BookPolicy
        # Freshness/identity policy at the original report; no current reference fills old gaps.
        policy=BookPolicy(**original.get('policy_as_of',{}))
        for r in db.execute('SELECT rowid,time_ms,payload FROM ip_inputs WHERE time_ms>? AND time_ms<=? AND rowid<=? ORDER BY time_ms,rowid',(start,end,evaluation.get('input_cursor',0))):
            for f in json.loads(r[2])['frames']:
                a=f['asset_id'];b=f.get('book') or {}
                if book_data_risk(f,policy,r[1]):continue
                mid=(__import__('decimal').Decimal(b['bid'])+__import__('decimal').Decimal(b['ask']))/2
                item=paths.setdefault(a,{'samples':0,'first':str(mid),'last':str(mid),'low':str(mid),'high':str(mid),'observations':[]})
                item.update(samples=item['samples']+1,last=str(mid),low=str(min(__import__('decimal').Decimal(item['low']),mid)),high=str(max(__import__('decimal').Decimal(item['high']),mid)))
                structure=original.get('assets',{}).get(a,{}).get('price_structure',{})
                high,low=structure.get('high'),structure.get('low')
                condition='ABOVE_ORIGINAL_RANGE' if high is not None and mid>__import__('decimal').Decimal(high) else 'BELOW_ORIGINAL_RANGE' if low is not None and mid<__import__('decimal').Decimal(low) else 'IN_OR_UNKNOWN_RANGE'
                if not item['observations'] or item['observations'][-1]['condition']!=condition:
                    item['observations'].append({'time_ms':r[1],'mid':str(mid),'condition':condition,'input_rowid':r[0]})
    finally:db.close()
    return {'original':original,'original_sha256':hashlib.sha256(raw).hexdigest(),'evaluation_as_of_ms':end,
            'subsequent_observed_paths':paths,'updated_markouts':{a:d['weighted_markouts'] for a,d in evaluation.get('assets',{}).items()},
            'account_now':evaluation.get('valuation'),'system_cases':evaluation.get('monitoring',{}).get('system_cases',[]),
            'not_known_at_original_time':True,'real_submission_calls':0,'limitations':['后续路径仅用于复盘，未回填原结论','没有有效后续样本时保持空白','价格变化不证明因果或最终赛果胜率']}
