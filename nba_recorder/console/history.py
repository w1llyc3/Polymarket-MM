"""Local registered session IDs; read-only projection, immutable report versions."""
import json
import re
import uuid
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
        return {'session_id':sid,'lifecycle':'HISTORICAL_READ_ONLY','mode':'SYNTHETIC_DEMO' if origin.get('data_source')=='SYNTHETIC_DATA' else 'OBSERVATION' if origin.get('execution_source')=='OBSERVATION_ONLY' else 'REAL_PAPER',
                'chart':{'start_ms':first[0] if first else None,'end_ms':last[0] if last else None},
                'state':{'books':[{'asset_id':f['asset_id'],'label':next(o['label'] for o in f['market']['outcomes'] if o['asset_id']==f['asset_id']),'book':f.get('book')} for f in frames],
                         'bound_market':frames[0]['market'] if frames else {},'risk_state':db.execute('SELECT state FROM pe_control WHERE id=1').fetchone()[0]},
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


def save_report(root,sid,kind,as_of=None,note=''):
    if kind not in {'Q4_MANUAL','Q4_AUTO','POST_SESSION','FINAL_UPDATE'}:raise ValueError('report type')
    if not isinstance(note,str) or len(note)>2000:raise ValueError('bounded note')
    path=resolve(root,sid);result=analyze(path,as_of)
    identity=uuid.uuid4().hex
    db=read_connection(path)
    try:origin=json.loads(db.execute("SELECT payload FROM ip_session_config WHERE id='console_origin'").fetchone()[0])
    finally:db.close()
    if result.get('execution_segments'):origin['execution_source']='OBSERVATION_THEN_PAPER'
    result.update(report_id=identity,session_id=sid,kind=kind,note=note,
                  report_basis='manual research request' if kind=='Q4_MANUAL' else kind,
                  source=origin,not_real_profit=True)
    target=Path(root)/(sid+'-report-'+identity+'.json')
    # Exclusive creation: late information can only create a new report.
    with target.open('x',encoding='utf-8') as f:json.dump(result,f,ensure_ascii=False,indent=2)
    markdown='# NBA 研究报告 · '+kind+'\n\n截至 '+str(result.get('as_of_ms'))+'；模拟结果，不是实盘收益。\n\n'
    markdown+='人工备注：'+note+'\n\n'
    markdown+='来源：'+str(result['source'])+'\n\n候选：'+result.get('candidate_plan','无数据')+'\n\n'
    markdown+='净方向 A−B：'+str(result.get('net_directional_shares'))+'；配对份数：'+str(result.get('paired_shares'))+'\n\n'
    markdown+='资金估值：'+json.dumps(result.get('valuation',{}),ensure_ascii=False)+'\n\n'
    markdown+='退出深度：'+json.dumps(result.get('exit_candidates',[]),ensure_ascii=False)+'\n\n'
    markdown+='样本：'+str(result.get('sample_size'))+'\n\n限制：'+'；'.join(result.get('limitations',[]))+'\n\n'
    markdown+='完整输入游标和统计见同名JSON。报告不可覆盖；新增数据需生成新版本。\n'
    with target.with_suffix('.md').open('x',encoding='utf-8') as f:f.write(markdown)
    return result
