"""One isolated offline worker; never receives the collector or its SQLite writer."""
import json
import re
import threading
import uuid
from pathlib import Path
from .engine import experiment

_guard=threading.Lock()
_running=False


def register(root, directory):
    """Expose a finished CLI experiment already inside the UI research folder."""
    folder=(Path(root)/'data/research').resolve()
    directory=Path(directory).resolve()
    if directory.parent!=folder or not re.fullmatch('[0-9a-f]{32}',directory.name):
        raise ValueError('Completed experiment must be data/research/<32-hex-id>')
    result=json.loads((directory/'comparison.json').read_text(encoding='utf-8'))
    if result.get('mode')!='OFFLINE_PAPER_RESEARCH' or result.get('live_go') is not False or result.get('real_submission_calls')!=0:
        raise ValueError('Explicit completed Paper result required')
    if not result.get('lanes') or any(not (directory/lane['lane']/'result.json').is_file() for lane in result['lanes']):
        raise ValueError('Incomplete lane exports')
    value={'id':directory.name,'status':'FINISHED',**{k:v for k,v in result.items() if k not in {'lanes','manifest'}},
        'lanes':[{k:v for k,v in lane.items() if k not in {'end_state','policy'}} for lane in result['lanes']]}
    with (folder/(directory.name+'.json')).open('x',encoding='utf-8') as fp:
        json.dump(value,fp,ensure_ascii=False,indent=2)
    return {'id':directory.name,'status':'FINISHED','url':'/offline#'+directory.name}


def start(root, body):
    global _running
    if set(body)-{'sample_id','synthetic','sensitivity','split_shares','budget','seconds'}:
        raise ValueError('Unsupported experiment fields')
    if type(body.get('synthetic',False)) is not bool or type(body.get('sensitivity',False)) is not bool:
        raise ValueError('Explicit experiment flags required')
    if bool(body.get('sample_id')) == bool(body.get('synthetic')):
        raise ValueError('Select registered sample or explicit synthetic demo')
    folder=Path(root)/'data'/'research'
    folder.mkdir(exist_ok=True)
    with _guard:
        if _running:raise ValueError('Offline experiment already running')
        identity=uuid.uuid4().hex
        job=folder/(identity+'.json')
        job.write_text(json.dumps({'id':identity,'status':'RUNNING'}),encoding='utf-8')
        _running=True
    def work():
        global _running
        try:
            scenarios=[{'name':n,'parameters':{},'queue_multiplier':q} for n,q in [('optimistic','.5'),('baseline','1'),('conservative','2')]] if body.get('sensitivity') else None
            result=experiment(root,folder/identity,body.get('sample_id'),body.get('synthetic',False),
                budget=body.get('budget','100'),split_shares=body.get('split_shares','0'),scenarios=scenarios,seconds=body.get('seconds'))
            value={'id':identity,'status':'FINISHED','tape_id':result['tape_id'],'sample':result['sample'],
                'budget_per_lane':result['budget_per_lane'],'explicit_split_shares':result['explicit_split_shares'],
                'code_commit':result['code_commit'],'working_tree_dirty':result['working_tree_dirty'],
                'limitations':result['limitations'],'lanes':[{k:v for k,v in lane.items() if k not in {'end_state','policy'}} for lane in result['lanes']]}
        except Exception as error:
            value={'id':identity,'status':'FAILED','error':type(error).__name__,'message':str(error)[:300]}
        finally:
            job.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
            with _guard:_running=False
    threading.Thread(target=work,name='offline-paper-research',daemon=True).start()
    return {'id':identity,'status':'RUNNING'}


def read(root,identity):
    if not re.fullmatch('[0-9a-f]{32}',identity):raise ValueError('Invalid experiment ID')
    return json.loads((Path(root)/'data/research'/(identity+'.json')).read_text(encoding='utf-8'))


def download(root,identity,lane,name):
    read(root,identity)
    if lane and not re.fullmatch(r'p[0-4]-L[012]-d[0-4]',lane):raise ValueError('Invalid lane')
    allowed={'comparison.csv','comparison.json'} if not lane else {'result.json','summary.md','orders.json','fills.json','markouts.json','quote-markouts.json','analysis.json','timeline.jsonl','decisions.jsonl','calibration.json'}
    if name not in allowed:raise ValueError('Invalid research export')
    path=Path(root)/'data/research'/identity
    if lane:path=path/lane
    path=path/name
    if path.is_symlink():raise ValueError('Local export only')
    return path


def plot(root,identity,lane):
    timeline=download(root,identity,lane,'timeline.jsonl')
    count=0;points=[];inventory=[];accepted={};events=[];last_inventory=None;last_phase={}
    last_price_time={};segments={}
    with timeline.open(encoding='utf-8') as fp:
        for line in fp:
            e=json.loads(line)
            if e['kind']=='PHASE_LOG':
                state=e['phase'].get('state','UNKNOWN')
                if last_phase.get(e['market_id'])!=state:
                    events.append({'t':e['time_ms'],'kind':'PHASE_'+state,'phase_ref':e['ref']})
                    last_phase[e['market_id']]=state
                continue
            if e['kind']=='ORDER_JOURNAL':
                j=e['journal'];p=j['payload'];order=p.get('order') or {}
                raw=p.get('control',{}).get('account')
                if raw:
                    a=json.loads(raw) if isinstance(raw,str) else raw
                    positions=a.get('positions',{})
                    if positions!=last_inventory:
                        inventory.append({'t':j['time_ms'],'positions':positions,'cash':a.get('cash'),'journal_seq':j['seq']});last_inventory=positions
                authority=(p.get('detail') or {}).get('authority') if isinstance(p.get('detail'),dict) else None
                if j['kind'] in {'EXECUTION_REPORT','RECONCILED'} and authority and authority.get('state') in {'OPEN','PARTIALLY_FILLED','FILLED','CANCELLED'}:
                    key=authority['id']
                    accepted.setdefault(key,{'order_id':key,'start_ms':j['time_ms'],'end_ms':None,'price':authority['price'],'asset':authority['asset_id'],'side':authority['side']})
                if order.get('id') in accepted and accepted[order['id']]['end_ms'] is None and order.get('state') in {'FILLED','CANCELLED','REJECTED','UNKNOWN','TIMEOUT','RECONCILED'}:
                    accepted[order['id']]['end_ms']=j['time_ms']
                if j['kind'] in {'PHASE_CHANGE','KILL_SWITCH','PAUSED','RECONCILIATION_BARRIER','RECONCILED'}:
                    events.append({'t':j['time_ms'],'kind':j['kind'],'journal_seq':j['seq']})
                continue
            if e['kind']!='INPUT':continue
            for f in e['frames']:
                b=f.get('book') or {}
                asset=f['asset_id']
                if b.get('bid') is None or b.get('ask') is None or b.get('flags') or not f.get('connected') or not 0<=e['time_ms']-f.get('book_received_ms',-1)<=3000:
                    last_price_time.pop(asset,None)
                    continue
                from ..fair_value.maths import decimal
                if asset not in last_price_time or e['time_ms']-last_price_time[asset]>3000:
                    segments[asset]=segments.get(asset,0)+1
                last_price_time[asset]=e['time_ms']
                points.append({'t':e['time_ms'],'asset':asset,'mid':str((decimal(b['bid'])+decimal(b['ask']))/2),'book_ref':f.get('book_ref'),'segment':segments[asset]})
                count+=1
    step=max(1,__import__('math').ceil(len(points)/2000))
    result=points[::step]
    if points and result[-1]!=points[-1]:result.append(points[-1])
    targets=[]
    with download(root,identity,lane,'decisions.jsonl').open(encoding='utf-8') as fp:
        for line in fp:
            decision=json.loads(line);f=decision['frame']
            for t in decision['targets']:
                targets.append({'t':f['time_ms'],'asset':f['asset_id'],'side':t['side'],'price':t['price'],'decision_ref':decision.get('decision_ref'),'purpose':t.get('purpose')})
    def bounded(values):
        stride=max(1,__import__('math').ceil(len(values)/2000))
        return {'values':values[::stride],'total':len(values),'stride':stride}
    return {'points':result,'total_points':count,'plot_sample_stride':step,'max_gap_ms':3000,
            'fills':json.loads(download(root,identity,lane,'fills.json').read_text(encoding='utf-8')),
            'inventory':bounded(inventory),'accepted_orders':bounded(list(accepted.values())),
            'targets':bounded(targets),'events':bounded(events),
            'downsampling_is_display_only':True}
