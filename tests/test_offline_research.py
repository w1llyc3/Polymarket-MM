import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from nba_recorder.research.engine import experiment, freeze_recording
from nba_recorder.research.catalog import inspect
from nba_recorder.inplay.store import SingleWriterStore
from nba_recorder.inplay.manager import UnifiedPaperManager
from nba_recorder.inplay.session import BookSession
from nba_recorder.inplay.demo import events, AT
from nba_recorder.execution.contract import DisabledLiveAdapter, preview_order, normalize_sdk_response
from nba_recorder.execution.account import public_positions
from nba_recorder.execution.remote_paper import RemotePaperAdapter
from nba_recorder.quote_engine.storage import PaperStore
from nba_recorder.inplay.policy import BookPolicy

ROOT=Path(__file__).resolve().parents[1]


class OfflineTests(unittest.TestCase):
    def test_streaming_freeze_matches_existing_hash_and_is_immutable(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as directory:
            first=PaperStore(Path(directory)/'a.sqlite');second=PaperStore(Path(directory)/'b.sqlite')
            stream=events();manifest={'mode':'BOOK_DRIVEN','source_kind':'SYNTHETIC_DATA'}
            try:
                one=first.seal(stream,manifest);two=second.seal_stream(iter(stream),manifest)
                self.assertEqual(one,two)
                self.assertEqual(first.verify(one),second.verify(two))
                with self.assertRaises(sqlite3.IntegrityError):second.db.execute("UPDATE qe_input SET payload='{}'")
                second.verify(two)
            finally:first.close();second.close()

    def test_grid_independent_reproducible_and_reconciled(self):
        with tempfile.TemporaryDirectory() as folder:
            a=experiment(ROOT,Path(folder)/'a',synthetic=True,split_shares='5')
            b=experiment(ROOT,Path(folder)/'b',synthetic=True,split_shares='5')
            self.assertEqual(a['tape_id'],b['tape_id'])
            self.assertEqual(len(a['lanes']),9)
            self.assertEqual([l['metrics'] for l in a['lanes']],[l['metrics'] for l in b['lanes']])
            self.assertGreater(len({l['metrics']['fill_events'] for l in a['lanes']}),1)
            for l in a['lanes']:
                m=l['metrics']; account=m['account']
                self.assertTrue(l['shutdown_ledger_consistent'])
                self.assertEqual(l['real_submission_calls'],0)
                if account['net_pnl'] is not None:
                    from decimal import Decimal
                    self.assertEqual(Decimal(account['equity'])-Decimal(account['capital_contributions']),Decimal(account['net_pnl']))
                self.assertTrue(all(x['status']=='PENDING' for x in m['markouts'] if x['horizon_s']>=60))
                self.assertIsNone(m['outcome_fair_edge'])

    def test_exact_recorded_callbacks_and_sample_period(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); p=root/'data/console'/'sample.sqlite';p.parent.mkdir(parents=True)
            store=SingleWriterStore(p)
            try:
                om=UnifiedPaperManager(store,start_ms=AT)
                with om.db:om.db.execute("INSERT INTO ip_session_config VALUES('console_origin',?)",(json.dumps({'data_source':'REAL_OBSERVED'}),))
                stream=events()[:3]
                BookSession(om).on_event({'id':'startup-empty-watchdog','kind':'WATCHDOG','time_ms':AT,'frames':[],'trades':[]})
                for e in stream:
                    for f in e['frames']:f['market']['game_start_ms']=AT+86400000
                    BookSession(om).on_event(e)
                original=[json.loads(r[0]) for r in om.db.execute('SELECT payload FROM ip_inputs ORDER BY time_ms,rowid')]
            finally:store.close()
            sample=inspect(root,p)
            self.assertEqual(sample['period'],'PRE_GAME')
            self.assertFalse(sample['full_game_verified'])
            target=root/'frozen.sqlite';identity=freeze_recording(p,target,sample)
            tape=PaperStore.readonly(target)
            try:
                restored=[e for row in tape.db.execute('SELECT payload FROM qe_input ORDER BY seq') for e in json.loads(row[0])['recorded_events']]
                self.assertEqual(restored,original)
                tape.verify(identity)
            finally:tape.close()

    def test_queue_assumption_restart_immutable(self):
        with tempfile.TemporaryDirectory() as folder:
            store=SingleWriterStore(Path(folder)/'paper.sqlite')
            try:
                om=UnifiedPaperManager(store,start_ms=AT)
                BookSession(om,queue_multiplier='.5')
                with self.assertRaises(ValueError):BookSession(om,queue_multiplier='2')
            finally:store.close()

    def test_order_preview_has_no_signature_or_sender(self):
        m={'venue':'polymarket.com','version':'v2','market_id':'m','outcomes':[{'asset_id':'1'}],'tick_size':'.01','minimum_order_size':'5'}
        preview=preview_order(m,{'asset_id':'1','side':'BUY','price':'.45','size':'5'})
        self.assertEqual(preview['identifier_kind'],'position_id');self.assertFalse(preview['sendable'])
        with self.assertRaises(ValueError):preview_order({**m,'venue':'polymarket.us'},{'asset_id':'1','side':'BUY','price':'.45','size':'5'})
        for method in ('submit','cancel','sign','transfer'):
            with self.assertRaises(PermissionError):getattr(DisabledLiveAdapter(),method)({})

    def test_public_account_is_not_authenticated_authority(self):
        import httpx
        requests=[]
        def handler(request):
            requests.append(request)
            return httpx.Response(200,json=[{'asset':'1','size':2,'conditionId':'c','avgPrice':.5}])
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result=public_positions('0x'+'1'*40,client,version='v1')
        self.assertEqual(len(requests),1);self.assertEqual(requests[0].method,'GET')
        self.assertIsNone(result['cash_balance']);self.assertIsNone(result['open_orders'])
        self.assertFalse(result['authenticated_account_connected'])
        with self.assertRaises(ValueError):public_positions('private-key')

    def test_sdk_acknowledgement_never_invents_fill_quantity(self):
        self.assertEqual(normalize_sdk_response({'ok':True,'order_id':'one','status':'live'},'intent')['state'],'OPEN')
        for status in ('matched','delayed','unmatched','new-unknown-status'):
            row=normalize_sdk_response({'ok':True,'order_id':'one','status':status,'trade_ids':['trade']},'intent')
            self.assertEqual(row['state'],'UNKNOWN');self.assertIsNone(row['filled_quantity'])
        self.assertEqual(normalize_sdk_response({'ok':False,'code':'INVALID_ORDER'},'intent')['state'],'REJECTED')
        self.assertEqual(normalize_sdk_response({'ok':True,'status':'live'},'intent')['state'],'UNKNOWN')


class SeparateAuthorityTests(unittest.TestCase):
    def test_remote_partial_cancel_timeout_and_restart(self):
        import urllib.request
        import test_quote_engine as fixture
        from nba_recorder.paper_execution.manager import OrderManager
        from nba_recorder.paper_execution.risk import ExecutionPolicy
        for scenario in ('partial_cancel','lost_submit','cancel_timeout','restart'):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                folder=Path(directory);ready=folder/'ready.json'
                with (folder/'server.log').open('w') as log:
                    process=subprocess.Popen([sys.executable,'-m','nba_recorder.execution.mock_server','--db',str(folder/'authority.sqlite'),'--ready',str(ready)],cwd=ROOT,stdout=log,stderr=log)
                    store=None
                    try:
                        deadline=time.monotonic()+15
                        while not ready.exists() and process.poll() is None and time.monotonic()<deadline:time.sleep(.05)
                        self.assertTrue(ready.exists())
                        port=json.loads(ready.read_text())['port'];path=folder/'oms.sqlite'
                        factory=lambda db:RemotePaperAdapter(db,port)
                        store=SingleWriterStore(path);policy=ExecutionPolicy(initial_inventory='10',target_inventory='10')
                        om=OrderManager(store,policy,fixture.AT,adapter_factory=factory)
                        target={'side':'BUY','price':'.49','size':'5','expires_ms':fixture.AT+30000,'maker_only':True}
                        order=om.enqueue('test-intent',target,fixture.frame(fixture.AT))
                        om.dispatch(drop_response=scenario=='lost_submit')
                        if scenario=='lost_submit':
                            self.assertIsNotNone(om.adapter.authoritative(order))
                            om.advance(fixture.AT+101)
                            self.assertEqual(om.order(order)['state'],'UNKNOWN')
                            self.assertNotEqual(om.risk_state,'ACTIVE')
                        else:
                            om.advance(fixture.AT+5)
                            if scenario=='partial_cancel':
                                om.cancel(order);om.fill(order,'2','unique-partial');om.advance(om.now)
                                self.assertEqual(om.order(order)['state'],'CANCEL_PENDING')
                                self.assertEqual(om.account()['positions']['a'],'12')
                                # Repeated RPC returns its original receipt; it must not add inventory again.
                                self.assertTrue(om.fill(order,'2','unique-partial'))
                                om.advance(om.now)
                                self.assertEqual(om.account()['positions']['a'],'12')
                                self.assertEqual(om.adapter.authoritative(order)['filled'],'2')
                                om.advance(om.now+10)
                                self.assertEqual(om.order(order)['state'],'CANCELLED')
                                self.assertTrue(om.consistent())
                            elif scenario=='cancel_timeout':
                                om.cancel(order,drop_response=True);om.advance(om.now+101)
                                self.assertEqual(om.order(order)['state'],'UNKNOWN')
                                self.assertEqual(om.adapter.authoritative(order)['state'],'CANCELLED')
                                self.assertNotEqual(om.risk_state,'ACTIVE')
                            else:
                                store.close();store=SingleWriterStore(path)
                                om=OrderManager(store,policy,fixture.AT,adapter_factory=factory)
                                self.assertNotEqual(om.risk_state,'ACTIVE')
                                self.assertEqual(om.order(order)['state'],'UNKNOWN')
                                om.reconcile()
                                self.assertNotEqual(om.risk_state,'ACTIVE')
                    finally:
                        if store:store.close()
                        if ready.exists():
                            request=urllib.request.Request(f'http://127.0.0.1:{port}/paper-rpc',data=json.dumps({'method':'shutdown','args':[],'kwargs':{}}).encode(),headers={'Content-Type':'application/json'})
                            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request,timeout=2):pass
                        else:process.terminate()
                        process.wait(timeout=5)


class OfflineHTTPTests(unittest.TestCase):
    def test_page_to_experiment_to_exports_and_plot(self):
        import threading,urllib.request
        from types import SimpleNamespace
        from nba_recorder.console.__main__ import make_server
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'data').mkdir()
            (root/'examples').mkdir()
            (root/'examples/account-read-fixture.json').write_bytes((ROOT/'examples/account-read-fixture.json').read_bytes())
            server=make_server(SimpleNamespace(root=root),0)
            worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
            base=f'http://127.0.0.1:{server.server_port}'
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
            def get(path):
                with opener.open(base+path,timeout=10) as response:return response.read()
            try:
                self.assertIn('离线回放'.encode(),get('/offline'))
                self.assertEqual(json.loads(get('/api/postcapture?date=2026-10-21'))['status'],'NOT_GENERATED')
                with self.assertRaises(urllib.error.HTTPError):get('/api/postcapture?date=../../secret')
                token=json.loads(get('/api/bootstrap'))['token']
                mock_request=urllib.request.Request(base+'/api/offline/account-mock',data=b'{}',headers={'Content-Type':'application/json','Origin':base,'X-Console-Token':token})
                with opener.open(mock_request,timeout=5) as response:mock=json.load(response)
                self.assertEqual(mock['mode'],'MOCK_AUTHENTICATED_READ_ONLY')
                self.assertEqual(mock['real_submission_calls'],0)
                self.assertEqual(len(mock['confirmed_fills']),1)
                request=urllib.request.Request(base+'/api/offline/run',data=json.dumps({'synthetic':True,'split_shares':'5','seconds':180}).encode(),headers={'Content-Type':'application/json','Origin':base,'X-Console-Token':token})
                with opener.open(request,timeout=5) as response:identity=json.load(response)['id']
                deadline=time.monotonic()+20
                result={}
                while time.monotonic()<deadline:
                    result=json.loads(get('/api/offline/job?id='+identity))
                    if result['status']!='RUNNING':break
                    time.sleep(.05)
                self.assertEqual(result['status'],'FINISHED',result)
                self.assertEqual(len(result['lanes']),9)
                self.assertIn(b'net_pnl',get('/api/offline/download?id='+identity+'&name=comparison.csv'))
                lane=result['lanes'][0]['lane']
                plot=json.loads(get('/api/offline/plot?id='+identity+'&lane='+lane))
                self.assertGreater(plot['total_points'],0)
                self.assertTrue(plot['downsampling_is_display_only'])
                self.assertIsInstance(json.loads(get('/api/offline/download?id='+identity+'&lane='+lane+'&name=quote-markouts.json')),list)
                with self.assertRaises(urllib.error.HTTPError):get('/api/offline/download?id='+identity+'&name=../../secret')
            finally:
                server.shutdown();server.server_close();worker.join(timeout=2)

    def test_order_lifecycle_uses_different_process_and_database(self):
        with tempfile.TemporaryDirectory() as folder:
            folder=Path(folder);ready=folder/'ready.json'
            with (folder/'server.log').open('w') as log:
                process=subprocess.Popen([sys.executable,'-m','nba_recorder.execution.mock_server','--db',str(folder/'authority.sqlite'),'--ready',str(ready)],cwd=ROOT,stdout=log,stderr=log)
                try:
                    deadline=time.monotonic()+15
                    while not ready.exists() and process.poll() is None and time.monotonic()<deadline:time.sleep(.05)
                    self.assertTrue(ready.exists(),(folder/'server.log').read_text())
                    port=json.loads(ready.read_text())['port']
                    store=SingleWriterStore(folder/'oms.sqlite')
                    try:
                        om=UnifiedPaperManager(store,start_ms=AT,adapter_factory=lambda db:RemotePaperAdapter(db,port))
                        stream=events();om.split(stream[0]['frames'][0]['market'],'5','explicit-mock-seed')
                        session=BookSession(om,'L1')
                        for e in stream:session.on_event(e)
                        self.assertTrue(om.consistent())
                        self.assertGreater(len(om.adapter.order_ids()),0)
                        self.assertEqual(om.db.execute('SELECT count(*) FROM pe_exchange').fetchone()[0],0)
                        self.assertEqual(om.db.execute('SELECT count(*) FROM pe_fills').fetchone()[0],0)
                        self.assertGreater(om.db.execute('SELECT count(*) FROM ip_receipts').fetchone()[0],0)
                        self.assertEqual(om.real_submission_calls,0)
                        om.kill('mock_end');om.advance(om.now+200);om.reconcile()
                        self.assertTrue(om.consistent())
                    finally:store.close()
                finally:
                    if ready.exists():
                        import urllib.request
                        request=urllib.request.Request(f'http://127.0.0.1:{port}/paper-rpc',data=json.dumps({'method':'shutdown','args':[],'kwargs':{}}).encode(),headers={'Content-Type':'application/json'})
                        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request,timeout=2):pass
                    else:process.terminate()
                    process.wait(timeout=5)
