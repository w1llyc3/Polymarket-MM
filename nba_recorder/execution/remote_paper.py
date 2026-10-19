import json
import urllib.request
from ..fair_value.maths import decimal


class RemotePaperAdapter:
    """OMS bridge to the independent loopback Paper authority, never to CLOB."""
    mode='PAPER'
    real_submission_calls=0

    def __init__(self,db,port):
        if type(port) is not int or not 1024<=port<=65535:raise ValueError('local Paper port required')
        self.db=db;self.url=f'http://127.0.0.1:{port}/paper-rpc'
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        db.execute('CREATE TABLE IF NOT EXISTS remote_paper_seen(port INTEGER,id INTEGER,PRIMARY KEY(port,id))')
        self.port=port

    def rpc(self,method,*args,**kwargs):
        request=urllib.request.Request(self.url,data=json.dumps({'method':method,'args':args,'kwargs':kwargs}).encode(),headers={'Content-Type':'application/json'})
        with self.opener.open(request,timeout=2) as response:result=json.load(response)
        if result.get('mode')!='PAPER' or result.get('real_submission_calls')!=0:raise ValueError('Not a Paper server')
        for pending in result['pending']:
            if self.db.execute('INSERT OR IGNORE INTO remote_paper_seen VALUES(?,?)',(self.port,pending['id'])).rowcount:
                self.db.execute('INSERT INTO pe_pending(due_ms,kind,order_id,payload) VALUES(?,?,?,?)',
                    (pending['due_ms'],pending['kind'],pending['order_id'],pending['payload']))
        return result['result']

    def submit(self,*args,**kwargs):return self.rpc('submit',*args,**kwargs)
    def cancel(self,*args,**kwargs):return self.rpc('cancel',*args,**kwargs)
    def apply_cancel(self,*args,**kwargs):return self.rpc('apply_cancel',*args,**kwargs)
    def authoritative(self,*args):return self.rpc('authoritative',*args)
    def order_ids(self):return self.rpc('order_ids')
    def fill(self,*args,**kwargs):return self.rpc('fill',*args,**kwargs)
    def balance_snapshot(self,*args):
        result=self.rpc('balance_snapshot',*args)
        return {'cash':decimal(result['cash']),'positions':{k:decimal(v) for k,v in result['positions'].items()}} if result else None
