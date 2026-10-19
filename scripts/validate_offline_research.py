"""Local API acceptance: no collection commands and no actual account requests."""
import json
import time
import urllib.request
from pathlib import Path

BASE='http://127.0.0.1:8765'
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))


def raw(path):
    with opener.open(BASE+path,timeout=20) as response:return response.read()


def get(path):return json.loads(raw(path))


if __name__=='__main__':
    token=get('/api/bootstrap')['token']
    samples=get('/api/offline/samples')
    before=get('/api/state')
    body={'synthetic':True,'split_shares':'5','budget':'100','seconds':180}
    request=urllib.request.Request(BASE+'/api/offline/run',data=json.dumps(body).encode(),headers={
        'Content-Type':'application/json','Origin':BASE,'X-Console-Token':token})
    with opener.open(request,timeout=5) as response:identity=json.load(response)['id']
    deadline=time.monotonic()+45
    while time.monotonic()<deadline:
        result=get('/api/offline/job?id='+identity)
        if result['status']!='RUNNING':break
        time.sleep(.1)
    assert result['status']=='FINISHED',result
    assert len(result['lanes'])==9
    lane=next(l for l in result['lanes'] if l['lane']=='p0-L1-d1')
    plot=get('/api/offline/plot?id='+identity+'&lane='+lane['lane'])
    assert plot['targets']['total']>0 and plot['accepted_orders']['total']>0 and plot['inventory']['total']>0
    assert b'net_pnl' in raw('/api/offline/download?id='+identity+'&name=comparison.csv')
    for file in ('orders.json','fills.json','markouts.json','quote-markouts.json','analysis.json','calibration.json'):
        json.loads(raw('/api/offline/download?id='+identity+'&lane='+lane['lane']+'&name='+file))
    assert all(l['shutdown_ledger_consistent'] and l['real_submission_calls']==0 for l in result['lanes'])
    after=get('/api/state')
    assert before['session_id']==after['session_id'] and before['lifecycle']==after['lifecycle']
    evidence={'source':'SYNTHETIC_DATA','job_id':identity,'url':BASE+'/offline#'+identity,
        'catalog_samples':len(samples['samples']),'catalog_errors':samples['errors'],
        'lanes':len(result['lanes']),'shared_tape':result['tape_id'],
        'all_lanes_ledger_consistent':True,'collector_session_unchanged':True,
        'plot':{k:plot[k]['total'] for k in ('targets','accepted_orders','inventory','events')},
        'exports_verified':True,'real_submission_calls':0,'authenticated_account_verified':False}
    folder=Path('evidence/offline-research');folder.mkdir(parents=True,exist_ok=True)
    (folder/'http-acceptance.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(evidence,ensure_ascii=False))
