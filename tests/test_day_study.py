import copy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from nba_recorder.inplay.sports import normalize
from nba_recorder.game_state import information
from nba_recorder.research.day_study import redecode,extract,draw,publish_html,IndexedHistoricalSession,run,merge_workers
from nba_recorder.inplay.demo import frame,AT,events
from nba_recorder.inplay.store import SingleWriterStore
from nba_recorder.inplay.manager import UnifiedPaperManager
from nba_recorder.inplay.session import BookSession


class SportsCaseTests(unittest.TestCase):
    def test_casefold_keeps_exact_game_and_team_checks_and_no_invented_clock(self):
        contract={'game_id':'123','event_id':'e','home_team':'det','away_team':'bos','identity_evidence':'fixture'}
        raw={'gameId':123,'leagueAbbreviation':'nba','homeTeam':'DET','awayTeam':'BOS','period':'Q1','elapsed':'11:42','live':True,'status':'InProgress'}
        for league in ('nba','NBA','Nba'):
            result=normalize({**raw,'leagueAbbreviation':league},AT,contract)
            self.assertIsNotNone(result['identity_evidence']);self.assertEqual(result['period'],1)
            self.assertIsNone(result['remaining_s']);self.assertEqual(result['clock_semantics'],'UNVERIFIED')
        for bad in ({'gameId':124},{'leagueAbbreviation':'wnba'},{'homeTeam':'BOS','awayTeam':'DET'}):
            self.assertIsNone(normalize({**raw,**bad},AT,contract)['identity_evidence'])
        market={'polymarket_game_id':123,'home_team':'det','away_team':'bos'}
        self.assertTrue(information(raw,market,AT,AT)['display_eligible'])
        for alias,code in (('NY','nyk'),('SA','sas'),('GS','gsw'),('NO','nop'),('PHO','phx')):
            mapped={**contract,'home_team':code}
            self.assertIsNotNone(normalize({**raw,'homeTeam':alias},AT,mapped)['identity_evidence'])
            self.assertIsNone(normalize({**raw,'awayTeam':alias,'homeTeam':'BOS'},AT,mapped)['identity_evidence'])

    def event(self):
        f=frame();f['market'].update(polymarket_game_id=123,home_team='det',away_team='bos')
        f['sports']={'received_ms':AT,'processed_ms':AT,'raw':{'gameId':123,'leagueAbbreviation':'nba','homeTeam':'DET','awayTeam':'BOS','period':'Q1','live':True,'status':'InProgress'},'identity_evidence':None}
        return {'id':'observed','time_ms':AT,'frames':[f],'trades':[]}

    def test_redecode_is_pure_causal_and_preserves_raw_provenance(self):
        event=self.event();before=copy.deepcopy(event);derived=redecode(event)
        self.assertEqual(event,before)
        self.assertIsNotNone(derived['frames'][0]['sports']['identity_evidence'])
        self.assertEqual(derived['frames'][0]['sports']['raw'],before['frames'][0]['sports']['raw'])
        self.assertIsNone(derived['frames'][0]['sports']['remaining_s'])
        for key in ('received_ms','processed_ms','strategy_available_ms'):
            future=self.event();future['frames'][0]['sports'][key]=AT+1
            self.assertIsNone(redecode(future)['frames'][0]['sports']['identity_evidence'])


class PosthocPermissionTests(unittest.TestCase):
    def test_explicit_paper_grant_preserves_transport_and_source(self):
        e=SportsCaseTests().event();f=e['frames'][0]
        f['connected']=False;f['market']['research_authorized']=False
        f['runtime_permission']={'quotes_enabled':False,'physical_connected':False,'epoch_matches':False}
        before=copy.deepcopy(e)
        derived=redecode(e,True)['frames'][0]
        self.assertEqual(e,before)
        self.assertFalse(derived['connected']);self.assertFalse(derived['runtime_permission']['physical_connected'])
        self.assertFalse(derived['runtime_permission']['epoch_matches'])
        self.assertTrue(derived['runtime_permission']['quotes_enabled']);self.assertTrue(derived['market']['research_authorized'])
        self.assertFalse(redecode(e)['frames'][0]['runtime_permission']['quotes_enabled'])

    def test_failed_capture_requires_durable_closed_recording(self):
        from nba_recorder.research.day_study import validate_closed
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'capture.sqlite';store=SingleWriterStore(path)
            try:
                om=UnifiedPaperManager(store,start_ms=AT);session=BookSession(om,'L2')
                session.on_event(events()[0])
                with self.assertRaises(ValueError):validate_closed({'status':'FAILED_NEEDS_REVIEW'},path)
                with om.db:om.db.execute("INSERT INTO ip_session_config VALUES('recording_finished_ms',?)",(str(AT),))
                om.kill('runner_window_ended');om.kill('runner_stopped_keep_halted')
                validate_closed({'status':'FAILED_NEEDS_REVIEW'},path)
                with self.assertRaises(ValueError):validate_closed({'status':'RUNNING'},path)
            finally:store.close()


class StudyHTTPTests(unittest.TestCase):
    def test_only_public_study_artifacts_are_served(self):
        import threading
        import urllib.request
        import urllib.error
        from types import SimpleNamespace
        from nba_recorder.console.__main__ import make_server
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);folder=root/'data/day-studies/2026-10-21/123'
            folder.mkdir(parents=True)
            (folder.parent/'index.html').write_text('posthoc paper',encoding='utf-8')
            (folder.parent/'terminal-valuation.json').write_text('{}',encoding='utf-8')
            (folder.parent/'terminal-valuation.csv').write_text('market_id,pnl',encoding='utf-8')
            (folder/'price-trades.png').write_bytes(b'png fixture')
            (folder/'source-tape.sqlite').write_bytes(b'private tape')
            strategy=root/'data/strategy-studies/inplay-test';strategy.mkdir(parents=True)
            (strategy/'index.html').write_text('independent paper',encoding='utf-8')
            (strategy/'paper.sqlite').write_bytes(b'private database')
            (strategy/'metrics.csv').write_text('model,pnl',encoding='utf-8')
            server=make_server(SimpleNamespace(root=root),0)
            worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
            base=f'http://127.0.0.1:{server.server_port}'
            try:
                def get(path):
                    with opener.open(base+path,timeout=5) as response:return response.read()
                self.assertEqual(get('/day-study/2026-10-21/'),b'posthoc paper')
                self.assertEqual(get('/day-study/2026-10-21/terminal-valuation.json'),b'{}')
                self.assertEqual(get('/day-study/2026-10-21/terminal-valuation.csv'),b'market_id,pnl')
                self.assertEqual(get('/day-study/2026-10-21/123/price-trades.png'),b'png fixture')
                self.assertIn(b'body',get('/day-study.css'))
                self.assertEqual(get('/strategy-study/inplay-test/'),b'independent paper')
                self.assertEqual(get('/strategy-study/inplay-test/metrics.csv'),b'model,pnl')
                for path in ('/strategy-study/inplay-test/paper.sqlite','/strategy-study/../manifest.json','/strategy-study/inplay-test/%2e%2e/index.html'):
                    with self.assertRaises(urllib.error.HTTPError) as caught:get(path)
                    self.assertEqual(caught.exception.code,404)
                for path in ('2026-10-21/123/source-tape.sqlite','2026-10-21/../secret','2026-02-30/'):
                    with self.assertRaises(urllib.error.HTTPError) as caught:get('/day-study/'+path)
                    self.assertEqual(caught.exception.code,404)
            finally:
                server.shutdown();server.server_close();worker.join(timeout=2)


class IndexedReplayTests(unittest.TestCase):
    def test_indexed_and_unfiltered_models_produce_identical_execution_journals(self):
        for model in ('L0','L1','L2'):
            snapshots=[]
            with tempfile.TemporaryDirectory() as directory:
                for index,session_type in enumerate((BookSession,IndexedHistoricalSession)):
                    store=SingleWriterStore(Path(directory)/(str(index)+'.sqlite'))
                    try:
                        om=UnifiedPaperManager(store,start_ms=AT)
                        om.split(frame()['market'],'5','same-test-capital')
                        session=session_type(om,model)
                        for event in events():session.on_event(event)
                        om.kill('same-end');om.advance(om.now+1000);om.reconcile()
                        snapshots.append({table:[tuple(r) for r in om.db.execute('SELECT * FROM '+table+' ORDER BY rowid')] for table in ('pe_orders','pe_exchange','pe_fills','l6_events','ip_fill_evidence')})
                        self.assertTrue(om.consistent())
                    finally:store.close()
                self.assertEqual(snapshots[0],snapshots[1],model)


@unittest.skipUnless(importlib.util.find_spec('matplotlib') and importlib.util.find_spec('numpy'),'install requirements-analysis.txt')
class StudyChartTests(unittest.TestCase):
    def test_completed_study_resumes_without_overwriting_lane_results(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'data/console';source.mkdir(parents=True)
            state=root/'data/unattended/2026-10-21/state.json';state.parent.mkdir(parents=True)
            state.write_text(json.dumps({'games':{'1':{'status':'FINISHED','session_id':'fixture'}}}))
            store=SingleWriterStore(source/'fixture.sqlite')
            try:
                om=UnifiedPaperManager(store,start_ms=AT);session=BookSession(om,'L2')
                for event in events():
                    for f in event['frames']:f['market']['question']='Synthetic test only'
                    session.on_event(event)
            finally:store.close()
            output=root/'study';run(root,'2026-10-21',output)
            before={str(p):p.read_bytes() for p in output.glob('1/*/result.json')}
            self.assertEqual(len(before),4)
            run(root,'2026-10-21',output,resume=True)
            self.assertEqual(before,{str(p):p.read_bytes() for p in output.glob('1/*/result.json')})
            self.assertTrue((output/'comparison.csv').exists())
            global_summary=(output/'summary.json').read_bytes()
            run(root,'2026-10-21',output,resume=True,market_id='1')
            self.assertEqual(global_summary,(output/'summary.json').read_bytes())
            self.assertEqual(json.loads((output/'summary-1.json').read_text())[0]['replay_status'],'FINISHED')
            self.assertEqual(len(merge_workers(output)),4)
            worker=json.loads((output/'summary-1.json').read_text());worker[0]['replay_status']='RUNNING'
            (output/'summary-1.json').write_text(json.dumps(worker))
            with self.assertRaises(ValueError):merge_workers(output)

    def test_projection_deduplicates_prints_and_draws_three_real_data_layouts(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);path=root/'sample.sqlite';store=SingleWriterStore(path)
            try:
                om=UnifiedPaperManager(store,start_ms=AT);session=BookSession(om,'L1')
                for e in events():session.on_event(e)
            finally:store.close()
            data=extract(path);self.assertEqual(len(data['assets']),2)
            self.assertEqual(sum(len(a['prints']) for a in data['assets'].values()),3)
            draw(data,root)
            for name in ('price-trades','depth-heatmap','liquidity-activity'):
                self.assertGreater((root/(name+'.png')).stat().st_size,1000)
            publish_html(root,[{'market_id':'1','question':'<unsafe>','lanes':[]}])
            text=(root/'index.html').read_text(encoding='utf-8')
            self.assertIn('&lt;unsafe&gt;',text);self.assertNotIn('<unsafe>',text)
            self.assertNotIn('<style>',text)


class TerminalValuationTests(unittest.TestCase):
    def test_terminal_equity_cash_inventory_and_no_mutation(self):
        from nba_recorder.research.terminal_valuation import terminal_value
        market={'market_id':'1','condition_id':'condition','outcomes':[{'asset_id':'a'},{'asset_id':'b'}]}
        raw={'event_type':'market_resolved','id':'1','market':'condition','assets_ids':['a','b'],'winning_asset_id':'a','timestamp':'100'}
        account={'cash':'21','capital_contributions':'25','positions':{'a':'5','b':'2'},'fees':'0','rewards':'0'}
        before=copy.deepcopy(account)
        result=terminal_value(account,market,raw,101)
        self.assertEqual(result['simulated_terminal_pnl'],'1');self.assertEqual(result['assumed_terminal_equity'],'26')
        self.assertEqual(account,before)
        for change in ({'winning_asset_id':'c'},{'market':'wrong'},{'id':'2'},{'assets_ids':['a','c']},{'timestamp':'102'}):
            with self.assertRaises(ValueError):terminal_value(account,market,{**raw,**change},101)
        self.assertEqual(terminal_value({**account,'positions':{'a':'0','b':'0'}},market,raw,101)['simulated_terminal_pnl'],'-4')


    def test_terminal_publication_reconciles_fifo_without_ledger_writes(self):
        import sqlite3
        from nba_recorder.research.terminal_valuation import publish
        from nba_recorder.inplay.report import report
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);output=root/'study';market=frame()['market'];key=market['market_id']
            lane=output/key/'REDECODED-L2';lane.mkdir(parents=True)
            store=SingleWriterStore(lane/'paper.sqlite')
            try:
                om=UnifiedPaperManager(store,start_ms=AT);session=BookSession(om,'L2')
                for event in events():session.on_event(event)
                account=report(om)['account']
                before=om.db.execute('SELECT count(*) FROM l6_events').fetchone()[0]
            finally:store.close()
            (root/'data/console').mkdir(parents=True)
            db=sqlite3.connect(root/'data/console/fixture-source.sqlite')
            db.execute('CREATE TABLE raw_events(event_key TEXT,local_receive_timestamp INTEGER,payload TEXT,event_type TEXT)')
            raw={'event_type':'market_resolved','id':key,'market':market['condition_id'],'assets_ids':[o['asset_id'] for o in market['outcomes']],
                 'winning_asset_id':market['outcomes'][0]['asset_id'],'winning_outcome':market['outcomes'][0]['label'],'timestamp':str(AT)}
            db.execute('INSERT INTO raw_events VALUES(?,?,?,?)',('resolution',AT,json.dumps(raw),'market_resolved'));db.commit();db.close()
            state=root/'data/unattended/2026-10-22/state.json';state.parent.mkdir(parents=True)
            state.write_text(json.dumps({'games':{key:{'market':market,'session_id':'fixture'}}}))
            (output/'summary.json').write_text(json.dumps([{'market_id':key,'question':'fixture','source_tape_id':'tape',
                'lanes':[{'mode':'REDECODED','model':'L2','account':account,'result':'REDECODED-L2/result.json',
                          'fills':1,'risk_state':'HALTED','derived_input_sha256':'hash'}]}]))
            result=publish(root,'2026-10-22',output)
            self.assertTrue(result['rows'][0]['fifo_terminal_valuation_reconciled'])
            db=sqlite3.connect(lane/'paper.sqlite')
            self.assertEqual(before,db.execute('SELECT count(*) FROM l6_events').fetchone()[0]);db.close()
            self.assertTrue((output/'terminal-valuation.csv').exists())
