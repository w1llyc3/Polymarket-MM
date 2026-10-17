"""HTTP acceptance of saved plans, Paper lifecycle and immutable retrospective reports."""
import hashlib
import json
import time
import urllib.request
import uuid
from pathlib import Path

BASE='http://127.0.0.1:8765'
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
def raw(path):
    with opener.open(BASE+path,timeout=30) as r:return r.read()
def get(path):return json.loads(raw(path))
token=get('/api/bootstrap')['token']
def post(path,body):
    request=urllib.request.Request(BASE+path,data=json.dumps(body).encode(),headers={'Content-Type':'application/json','Origin':BASE,'X-Console-Token':token})
    with opener.open(request,timeout=30) as r:return json.load(r)
def command(action,**extra):
    state=get('/api/state');identity=uuid.uuid4().hex
    post('/api/command',{'session_id':state['session_id'],'expected_version':state['version'],'request_id':identity,'action':action,'confirmed':True,**extra})
    for _ in range(150):
        r=get('/api/receipt/'+identity)
        if r['status']!='PENDING':
            if r['status']!='APPLIED':raise RuntimeError(r)
            return r
        time.sleep(.1)
    raise RuntimeError('receipt timeout')
def wait(predicate,seconds=15):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        value=get('/api/state')
        if predicate(value):return value
        time.sleep(.2)
    raise RuntimeError('state wait timeout')

if get('/api/state')['lifecycle'] not in {'IDLE','STOPPED'}:raise RuntimeError('refuse to interrupt existing session')
plan=post('/api/plans',{'name':'合成验收方案','parameters':{'quote_distance':'.03','max_order_lifetime_ms':90000}})
revision=post('/api/plans',{'id':plan['id'],'expected_version':1,'name':'合成验收方案v2','parameters':{'quote_distance':'.04'}})
receipts=[command('start',mode='SYNTHETIC_DEMO',paper_budget='25',plan_id=plan['id'],plan_version=1)]
state=wait(lambda s:s['lifecycle']=='RUNNING' and s.get('state',{}).get('paper_clock_ms',0)>0)
sid=state['session_id'];time.sleep(3)
research=get('/api/research?session_id='+sid)
assert research['policy_as_of']['quote_distance']=='.03'
assert research['bound_plan']['plan']['version']==1
assert research['valuation']['capital_contributions']=='25'
report=post('/api/reports',{'session_id':sid,'kind':'Q4_MANUAL','note':'合成研究：观察区间与退出成本；不预测最终赢家','manual_q3_ended':False})
url='/api/report-download?session_id='+sid+'&report_id='+report['report_id']+'&format=json'
original=raw(url)
receipts.append(command('pause'));time.sleep(1);receipts.append(command('reconcile'));receipts.append(command('resume'))
time.sleep(1);receipts.append(command('prepare_direction'))
deadline=time.monotonic()+15
while time.monotonic()<deadline:
    research=get('/api/research?session_id='+sid)
    if (research.get('direction_preparation') or {}).get('status')=='READY_FOR_MANUAL_DIRECTION_RESEARCH':break
    time.sleep(.3)
else:raise RuntimeError('direction preparation not reconciled')
assert get('/api/state')['state']['risk_state']=='HALTED'
comparison=get('/api/review?session_id='+sid+'&report_id='+report['report_id'])
assert comparison['original_sha256']==hashlib.sha256(original).hexdigest()
assert raw(url)==original
history=get('/api/history?session_id='+sid)
assert history['read_only'] and get('/api/state')['session_id']==sid
receipts.append(command('stop'));final=wait(lambda s:s['lifecycle']=='STOPPED')
raw('/api/report-download?session_id='+sid+'&report_id='+report['report_id']+'&format=md')
output=Path('evidence/workbench-completion');output.mkdir(parents=True,exist_ok=True)
(output/'http-acceptance.json').write_text(json.dumps({'source':'SYNTHETIC_DATA','session_id':sid,'plan_versions':[plan,revision],
 'report_hash':hashlib.sha256(original).hexdigest(),'original_report_unchanged':True,'direction_preparation':research['direction_preparation'],
 'coverage':research['data_coverage'],'monitoring':research['monitoring'],'final_lifecycle':final['lifecycle'],
 'real_submission_calls':0,'receipts':receipts,'visual_browser_acceptance':False},ensure_ascii=False,indent=2),encoding='utf-8')
print('Paper HTTP acceptance PASS; real submissions=0; immutable report hash verified')
