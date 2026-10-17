"""Local HTTP smoke test, exclusively synthetic Paper. Never contacts an order API."""
import json
import time
import uuid
import urllib.request
from pathlib import Path

opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
base='http://127.0.0.1:8765'
def get(path):
    with opener.open(base+path,timeout=10) as r:return json.load(r)
token=get('/api/bootstrap')['token']
def post(path,body):
    req=urllib.request.Request(base+path,data=json.dumps(body).encode(),headers={'Content-Type':'application/json','Origin':base,'X-Console-Token':token})
    with opener.open(req,timeout=30) as r:return json.load(r)
def command(action,**extra):
    state=get('/api/state');identity=uuid.uuid4().hex
    post('/api/command',{'action':action,'request_id':identity,'session_id':state['session_id'],'expected_version':state['version'],'confirmed':True,**extra})
    for _ in range(100):
        receipt=get('/api/receipt/'+identity)
        if receipt['status']!='PENDING':return receipt
        time.sleep(.1)
    raise RuntimeError('receipt timeout')
state=get('/api/state')
if state['lifecycle'] not in {'IDLE','STOPPED'}:raise RuntimeError('refuse to touch existing running session')
receipts=[command('start',mode='SYNTHETIC_DEMO')]
assert receipts[-1]['status']=='APPLIED'
for _ in range(80):
    state=get('/api/state')
    if state['lifecycle']=='STOPPED':break
    time.sleep(1)
assert state['lifecycle']=='STOPPED',state['lifecycle']
sid=state['session_id'];old=get('/api/history?session_id='+sid)
analysis=get('/api/research?session_id='+sid)
report=post('/api/reports',{'session_id':sid,'kind':'Q4_MANUAL','note':'Synthetic HTTP smoke only; not a real Q4 observation'})
assert old['read_only'] and analysis['real_submission_calls']==0
assert get('/api/state')['session_id']==sid
out=Path('evidence/next-iteration');out.mkdir(parents=True,exist_ok=True)
(out/'http-smoke.json').write_text(json.dumps({'source':'SYNTHETIC_DATA','session_id':sid,'lifecycle':state['lifecycle'],
 'history_readonly':old['read_only'],'analysis_as_of':analysis['as_of_ms'],'report_id':report['report_id'],
 'paper_fills':analysis['sample_size']['paper_fills'],'real_submission_calls':0,'receipts':receipts},ensure_ascii=False,indent=2),encoding='utf-8')
print('Synthetic console HTTP smoke PASS; real submissions 0')
