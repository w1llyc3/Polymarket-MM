import csv
import json
import subprocess
import time
from dataclasses import asdict
from pathlib import Path
from ..fair_value.store import read_connection, digest
from ..fair_value.maths import decimal
from ..quote_engine.storage import PaperStore
from ..inplay.store import SingleWriterStore
from ..inplay.manager import UnifiedPaperManager
from ..inplay.policy import BookPolicy
from ..inplay.session import BookSession
from ..inplay.report import report
from ..inplay.demo import events as demo_events
from .catalog import resolve


def freeze_recording(path, target, sample):
    """Freeze exact strategy-visible callbacks, retaining within-ms callback order.

    Risk/permission frames are source evidence, not newly granted permissions.
    Missing sports information is never replaced with a synthetic phase.
    """
    db = read_connection(path)
    out = PaperStore(target)
    try:
        db.execute('BEGIN')
        def buckets():
            at,bucket=None,[]
            for row in db.execute('SELECT time_ms,payload FROM ip_inputs WHERE time_ms BETWEEN ? AND ? ORDER BY time_ms,rowid',(sample['start_ms'],sample['end_ms'])):
                if row['time_ms'] != at and bucket:
                    yield {'id':digest(['bucket',at,bucket]),'time_ms':at,'frames':[],'trades':[],'recorded_events':bucket}
                    bucket=[]
                at=row['time_ms'];bucket.append(json.loads(row['payload']))
            if bucket:yield {'id':digest(['bucket',at,bucket]),'time_ms':at,'frames':[],'trades':[],'recorded_events':bucket}
        return out.seal_stream(buckets(), {'mode': 'BOOK_DRIVEN', 'source_kind': sample['source'],
            'sample': sample, 'bucketed_recorded_events': True, 'start_ms': sample['start_ms'],
            'end_ms': sample['end_ms'], 'phase_policy': 'original observed authority only', 'network_calls': 0})
    finally:
        out.close(); db.close()


def seal_demo(path,seconds=None,cadence_ms=1000):
    store = PaperStore(path)
    try:
        if seconds is not None:
            from .scale import events
            from ..inplay.demo import AT
            return store.seal_stream(events(seconds,cadence_ms),{'mode':'BOOK_DRIVEN','source_kind':'SYNTHETIC_DATA',
                'start_ms':AT,'end_ms':AT+seconds*1000,'network_calls':0,'synthetic_scale_seconds':seconds,
                'cadence_ms':cadence_ms,'limitation':'Stretched synthetic phases, not a real NBA clock or actual game'})
        stream = demo_events()
        return store.seal(stream, {'mode':'BOOK_DRIVEN', 'source_kind':'SYNTHETIC_DATA',
            'start_ms':stream[0]['time_ms'], 'end_ms':stream[-1]['time_ms'], 'network_calls':0})
    finally:
        store.close()


def observed_events(source, tape_id):
    for row in source.db.execute('SELECT payload FROM qe_input WHERE tape_id=? ORDER BY seq', (tape_id,)):
        item = json.loads(row[0])
        for event in item.get('recorded_events', [item]):
            if event['time_ms'] != item['time_ms']:
                raise ValueError('callback outside frozen bucket')
            yield event


def experiment(root, output, sample_id=None, synthetic=False, distances=('.01','.02','.03'),
               models=('L0','L1','L2'), budget='100', split_shares='0', scenarios=None, seconds=None,
               synthetic_seconds=None,cadence_ms=1000):
    started=time.perf_counter();cpu_started=time.process_time()
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    if bool(sample_id) == bool(synthetic):
        raise ValueError('Select one registered sample or explicit synthetic demo')
    # Parameters reuse the same validation as cross-game research plans.
    from ..console.plans import validate
    if not decimal(budget).is_finite() or not 1 <= decimal(budget) <= 1000:
        raise ValueError('virtual budget must be 1..1000')
    if not decimal(split_shares).is_finite() or not 0 <= decimal(split_shares) <= min(decimal(budget), decimal('15')):
        raise ValueError('explicit cash-funded split must be 0..15 and affordable')
    if not 1 <= len(distances) <= 5 or not models or set(models)-{'L0','L1','L2'}:
        raise ValueError('bounded distance/model grid required')
    scenarios = scenarios or [{'name':'base', 'parameters':{}, 'queue_multiplier':'1'}]
    if not 1 <= len(scenarios) <= 5:
        raise ValueError('at most five sensitivity profiles')
    for scenario in scenarios:
        validate(scenario.get('parameters', {}))
        if not 0 <= decimal(scenario.get('queue_multiplier','1')) <= 10:
            raise ValueError('queue multiplier must be 0..10')
    tape_path = output/'tape.sqlite'
    sample = {'source':'SYNTHETIC_DATA', 'research_phase':False, 'period':'SYNTHETIC', 'full_game_verified':False}
    if synthetic:
        tape_id = seal_demo(tape_path,synthetic_seconds,cadence_ms)
        if synthetic_seconds is not None:sample.update(synthetic_scale_seconds=synthetic_seconds,cadence_ms=cadence_ms)
    else:
        path, sample = resolve(root, sample_id)
        if seconds is not None:
            if type(seconds) is not int or not 1<=seconds<=28800:raise ValueError('selected replay window must be 1..28800 seconds')
            sample={**sample,'original_end_ms':sample['end_ms'],'end_ms':min(sample['end_ms'],sample['start_ms']+seconds*1000),
                    'selected_window_s':seconds,'window_starts_at_first_record':True}
        tape_id = freeze_recording(path, tape_path, sample)
    source = PaperStore.readonly(tape_path)
    try:
        manifest = source.verify(tape_id)
        lanes = []
        for profile_index, scenario in enumerate(scenarios):
            for model in models:
                for distance_index, distance in enumerate(distances):
                    parameters = {**scenario.get('parameters', {}), 'quote_distance': str(distance)}
                    validate(parameters)
                    delays=scenario.get('latencies',{})
                    if set(delays)-{'submit_ack_ms','cancel_latency_ms','data_transport_ms'} or any(type(v) is not int or not 0<=v<=1000 for v in delays.values()):
                        raise ValueError('Paper delay hypotheses must be integer milliseconds 0..1000')
                    policy = BookPolicy(initial_cash=str(budget), research_phase=sample['research_phase'], **parameters,**delays)
                    lane_id = f'p{profile_index}-{model}-d{distance_index}'
                    lane_dir = output/lane_id
                    lane_dir.mkdir()
                    lane_started=time.perf_counter();lane_cpu=time.process_time()
                    store = SingleWriterStore(lane_dir/'paper.sqlite')
                    try:
                        om = UnifiedPaperManager(store, policy, manifest['start_ms'])
                        session = BookSession(om, model, queue_multiplier=scenario.get('queue_multiplier','1'))
                        seeded = False
                        for event in observed_events(source, tape_id):
                            if not seeded and event['frames']:
                                if decimal(split_shares):
                                    om.split(event['frames'][0]['market'], split_shares, 'explicit-research-cash-funded-split')
                                seeded = True
                            session.on_event(event)
                        result = report(om)
                        # Retain end-of-input state for study, then separately cancel and reconcile.
                        end_state = {'risk_state': om.risk_state, 'orders': om.orders(), 'account': result['account']}
                        om.kill('offline_replay_end_cancel_not_liquidate')
                        om.advance(om.now+max(policy.cancel_latency_ms,policy.request_timeout_ms)+1)
                        om.reconcile()
                        from ..inplay.__main__ import save_report
                        save_report(om, result)
                        from .review import export_review
                        metrics = export_review(om, lane_dir, result, manifest['end_ms'])
                        row = {'lane':lane_id,'model':model, 'distance_from_book_mid':str(distance),
                            'scenario':scenario['name'], 'queue_multiplier':scenario.get('queue_multiplier','1'),
                            'policy':asdict(policy), 'tape_id':tape_id, 'sample_source':sample['source'],
                            'outcome_fair_edge':None, 'end_state':end_state, 'metrics':metrics,
                            'shutdown_ledger_consistent':om.consistent(), 'real_submission_calls':0}
                        (lane_dir/'result.json').write_text(json.dumps(row,ensure_ascii=False,indent=2),encoding='utf-8')
                        lanes.append(row)
                    finally:
                        store.close()
                    from .scale import peak_rss_bytes
                    row['performance']={'wall_seconds':time.perf_counter()-lane_started,'cpu_seconds':time.process_time()-lane_cpu,
                        'process_peak_rss_bytes':peak_rss_bytes(),'database_bytes':(lane_dir/'paper.sqlite').stat().st_size,
                        'output_bytes':sum(p.stat().st_size for p in lane_dir.iterdir() if p.is_file())}
                    (lane_dir/'result.json').write_text(json.dumps(row,ensure_ascii=False,indent=2),encoding='utf-8')
                    print(f"Paper lane {lane_id} done: {row['performance']['wall_seconds']:.2f}s",flush=True)
        try:
            commit = subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True,stderr=subprocess.DEVNULL).strip()
            dirty=bool(subprocess.check_output(['git','status','--porcelain'],cwd=root,text=True,stderr=subprocess.DEVNULL).strip())
        except (OSError,subprocess.SubprocessError):
            commit = 'UNKNOWN'
            dirty=None
        summary = {'mode':'OFFLINE_PAPER_RESEARCH','sample':sample,'tape_id':tape_id,'manifest':manifest,
            'code_commit':commit, 'working_tree_dirty':dirty, 'budget_per_lane':budget, 'explicit_split_shares':split_shares,
            'lanes':lanes, 'live_go':False,'real_submission_calls':0,
            'limitations':['Book-mid distance is not outcome fair-value edge', 'Models are uncalibrated proxies, not real fills',
                           'Independent lane capital; do not sum lanes as one account profit',
                           'No full-game certification; PRE_GAME samples cannot validate in-play strategies']}
        from .scale import peak_rss_bytes
        summary['performance']={'wall_seconds':time.perf_counter()-started,'cpu_seconds':time.process_time()-cpu_started,
            'process_peak_rss_bytes':peak_rss_bytes(),'output_bytes_before_comparison':sum(p.stat().st_size for p in output.rglob('*') if p.is_file()),
            'basis':'Sequential lanes; RSS is process lifetime peak, not sum of lane peaks'}
        (output/'comparison.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
        with (output/'comparison.csv').open('w',newline='',encoding='utf-8-sig') as fp:
            writer=csv.writer(fp)
            writer.writerow(['lane','model','book_mid_distance','profile','fills','net_pnl','equity','quote_availability','ledger_consistent'])
            for lane in lanes:
                m=lane['metrics']; writer.writerow([lane['lane'],lane['model'],lane['distance_from_book_mid'],lane['scenario'],
                    m['fill_events'],m['account']['net_pnl'],m['account']['equity'],m['quote_availability'],lane['shutdown_ledger_consistent']])
        return summary
    finally:
        source.close()
