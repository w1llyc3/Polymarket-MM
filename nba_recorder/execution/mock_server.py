"""Separate-process local Paper authority. Public exchange APIs are never called."""
import argparse
import json
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from ..paper_execution.store import ExecutionStore
from ..paper_execution.adapter import PaperExchangeAdapter
from ..fair_value.store import digest


def serve(path, port, ready):
    store=ExecutionStore(path)
    adapter=PaperExchangeAdapter(store.db)
    store.db.execute('CREATE TABLE IF NOT EXISTS mock_rpc(id TEXT PRIMARY KEY, result TEXT)')
    methods={'submit','cancel','apply_cancel','authoritative','order_ids','fill','balance_snapshot','shutdown'}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            try:
                if self.path!='/paper-rpc' or self.headers.get('Host') not in {f'127.0.0.1:{self.server.server_port}'}:
                    raise ValueError('Local mock only')
                n=int(self.headers.get('Content-Length','0'))
                if not 0<n<=262144:raise ValueError('Bounded payload required')
                q=json.loads(self.rfile.read(n)); method=q['method']; args=q['args']; kwargs=q['kwargs']
                if method not in methods:raise ValueError('Unsupported method')
                identity=digest([method,args,kwargs])
                mutating=method in {'submit','cancel','apply_cancel','fill'}
                with store.db:
                    old=store.db.execute('SELECT result FROM mock_rpc WHERE id=?',(identity,)).fetchone() if mutating else None
                    result=None if method=='shutdown' else json.loads(old[0]) if old else getattr(adapter,method)(*args,**kwargs)
                    # The mock outbox is immutable to the client and replayable after lost HTTP response.
                    if mutating and not old:
                        store.db.execute('INSERT INTO mock_rpc VALUES(?,?)',(identity,json.dumps(result,default=str)))
                    pending=[dict(r) for r in store.db.execute('SELECT * FROM pe_pending ORDER BY id')]
                raw=json.dumps({'mode':'PAPER','result':result,'pending':pending,'real_submission_calls':0},default=str).encode()
                self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
                if method=='shutdown':__import__('threading').Thread(target=self.server.shutdown,daemon=True).start()
            except (ValueError,KeyError,TypeError):
                self.send_error(400,'Invalid local Paper RPC')
    server=HTTPServer(('127.0.0.1',port),Handler)
    Path(ready).write_text(json.dumps({'port':server.server_port,'pid':__import__('os').getpid(),'mode':'PAPER'}),encoding='utf-8')
    try:server.serve_forever(poll_interval=.05)
    finally:server.server_close();store.close()


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--db',required=True);p.add_argument('--port',type=int,default=0);p.add_argument('--ready',required=True)
    a=p.parse_args();serve(a.db,a.port,a.ready)
