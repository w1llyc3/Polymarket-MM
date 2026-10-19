import argparse
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from .contract import preview_order
from .account import public_positions
from .remote_paper import RemotePaperAdapter


def mock_demo(directory):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=False)
    ready=directory/'ready.json'
    root=Path(__file__).resolve().parents[2]
    with (directory/'mock-server.log').open('w',encoding='utf-8') as log:
        process=subprocess.Popen([sys.executable,'-m','nba_recorder.execution.mock_server','--db',str(directory/'authority.sqlite'),
            '--ready',str(ready)],cwd=root,stdin=subprocess.DEVNULL,stdout=log,stderr=log,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        try:
            deadline=time.monotonic()+15
            while not ready.exists() and process.poll() is None and time.monotonic()<deadline:time.sleep(.05)
            if not ready.exists():raise RuntimeError('Local Mock did not start; see mock-server.log')
            port=json.loads(ready.read_text())['port']
            from ..inplay.store import SingleWriterStore
            from ..inplay.manager import UnifiedPaperManager
            from ..inplay.session import BookSession
            from ..inplay.demo import events, AT
            from ..inplay.report import report
            store=SingleWriterStore(directory/'oms.sqlite')
            try:
                om=UnifiedPaperManager(store,start_ms=AT,adapter_factory=lambda db:RemotePaperAdapter(db,port))
                stream=events();om.split(stream[0]['frames'][0]['market'],'5','explicit-synthetic-funded-split')
                session=BookSession(om,'L1')
                for event in stream:session.on_event(event)
                result=report(om)
                om.kill('end_mock_demo');om.advance(om.now+200);om.reconcile()
                result.update(source='SYNTHETIC_DATA',authority_separate_process=True,authority_separate_database=True,
                    shutdown_ledger_consistent=om.consistent(),real_submission_calls=0)
                (directory/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
                return {'mode':'INDEPENDENT_PROCESS_MOCK','orders':result['orders'],'fill_events':result['fill_events'],
                    'ledger_consistent':result['shutdown_ledger_consistent'],'real_submission_calls':0,'live_go':False}
            finally:store.close()
        finally:
            if ready.exists() and process.poll() is None:
                port=json.loads(ready.read_text())['port']
                request=urllib.request.Request(f'http://127.0.0.1:{port}/paper-rpc',data=json.dumps({'method':'shutdown','args':[],'kwargs':{}}).encode(),headers={'Content-Type':'application/json'})
                with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request,timeout=2):pass
            elif process.poll() is None:process.terminate()
            process.wait(timeout=5)


def main():
    p=argparse.ArgumentParser(description='Offline execution preparation; no live sending or signing')
    sub=p.add_subparsers(dest='command',required=True)
    preview=sub.add_parser('preview');preview.add_argument('--file',type=Path,required=True)
    account=sub.add_parser('account');account.add_argument('--address',required=True,help='Public address only, no key')
    demo=sub.add_parser('mock-demo');demo.add_argument('--out',type=Path,required=True)
    a=p.parse_args()
    if a.command=='preview':
        from ..fair_value.providers import reject_secrets
        value=json.loads(a.file.read_text(encoding='utf-8'));reject_secrets(value)
        result=preview_order(value['market'],value['target'])
    elif a.command=='account':result=public_positions(a.address)
    else:result=mock_demo(a.out.resolve())
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
