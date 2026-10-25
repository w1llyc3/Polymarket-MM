"""Independent closed-real-sample campaign. No network or live adapter allowed."""
import argparse
import csv
import hashlib
import html
import json
import socket
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch
from decimal import Decimal as D

from ..fair_value.store import read_connection
from ..fill_management.ledger import valuation
from ..inplay.policy import BookPolicy
from ..inplay.manager import UnifiedPaperManager
from ..inplay.store import SingleWriterStore
from ..inplay.reference import book_data_risk
from ..inplay.research_phase import permission
from ..inplay.observations import price_point
from ..inplay.report import report
from ..console.research import markouts
from ..quote_engine.storage import PaperStore
from ..storage import encode
from .catalog import catalog
from .day_study import redecode, IndexedHistoricalSession
from .engine import freeze_recording, observed_events
from .terminal_valuation import terminal_value


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def profile(distance='.02'):
    return BookPolicy(initial_cash='25', research_phase=True, quote_profile='INPLAY_ROTATION_V1',
                      quote_distance=distance, inventory_skew='.002', max_one_sided_shares='10')


def protected_files(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for day in ('2026-10-21','2026-10-22')
            for p in (root/'data/day-studies'/day).rglob('*')
            if p.is_file() and p.suffix in {'.json','.csv','.html','.png'}}


def prepare(root, output, distance='.02'):
    output.mkdir(parents=True, exist_ok=False)
    dump(output/'policy.json', asdict(profile(distance)))
    dump(output/'old-results-sha256.json', protected_files(root))
    collection=catalog(root); samples=[]; excluded=[]; old={}
    for summary in (root/'data/day-studies').glob('*/summary.json'):
        for item in json.loads(summary.read_text(encoding='utf-8')):
            if item.get('replay_status')=='FINISHED':old[item['session_id']]=(summary.parent,item)
    for sample in collection['samples']:
        if sample['source']!='REAL_OBSERVED':continue
        path=root/'data'/sample['path'];sid=path.stem
        db=read_connection(path)
        try:
            stop=db.execute("SELECT max(time_ms) FROM pe_journal WHERE kind='KILL_SWITCH' AND json_extract(payload,'$.detail.reason')='runner_stopped_keep_halted'").fetchone()[0]
            original={'orders':db.execute('SELECT count(*) FROM pe_orders').fetchone()[0],
                      'fills':db.execute('SELECT count(*) FROM pe_fills').fetchone()[0]}
            first=db.execute("SELECT payload FROM ip_inputs WHERE json_array_length(payload,'$.frames')>0 ORDER BY time_ms,rowid LIMIT 1").fetchone()
        finally:db.close()
        reason='NO_USABLE_MARKET_FRAMES' if not first else 'RECORDING_NOT_DURABLY_CLOSED' if stop is None or stop<sample['end_ms'] else None
        if reason:
            excluded.append({**sample,'reason':reason});continue
        market=json.loads(first[0])['frames'][0]['market'];folder=output/sid;folder.mkdir()
        item={**sample,'session_id':sid,'market':market,'original_counts':original}
        if sid in old:
            parent,prior=old[sid];tape=parent/str(market['market_id'])/'source-tape.sqlite'
            item.update(tape=str(tape),tape_id=prior['source_tape_id'],old_result_folder=str(parent/str(market['market_id'])),source_input_sha256=prior['source_input_sha256'])
        else:
            tape=folder/'source-tape.sqlite';item.update(tape=str(tape),tape_id=freeze_recording(path,tape,sample))
        frozen=PaperStore.readonly(tape);sha=hashlib.sha256();n=0
        try:
            frozen.verify(item['tape_id'])
            for e in observed_events(frozen,item['tape_id']):sha.update((str(e['time_ms'])+'\t'+encode(e)+'\n').encode());n+=1
        finally:frozen.close()
        if n!=sample['input_records'] or item.get('source_input_sha256',sha.hexdigest())!=sha.hexdigest():raise ValueError('Frozen source mismatch: '+sid)
        item['source_input_sha256']=sha.hexdigest()
        # Check original callback bytes separately; old caches cannot hide changed originals.
        db=read_connection(path);actual=hashlib.sha256()
        try:
            for r in db.execute('SELECT time_ms,payload FROM ip_inputs ORDER BY time_ms,rowid'):actual.update((str(r['time_ms'])+'\t'+r['payload']+'\n').encode())
        finally:db.close()
        if actual.hexdigest()!=sha.hexdigest():raise ValueError('Original input differs from frozen tape')
        samples.append(item);print('PREPARED '+sid+' '+market.get('question',''),flush=True)
    manifest={'mode':'INDEPENDENT_POSTHOC_PAPER','profile':asdict(profile(distance)), 'samples':samples,
              'excluded':excluded,'catalog_errors':collection['errors'],'created_ms':int(time.time()*1000),
              'old_results_unchanged':True,'real_submission_calls':0,'live_go':False}
    dump(output/'manifest.json',manifest)
    return manifest


def resolution(root,sample,account):
    path=root/'data/console'/(sample['session_id']+'-source.sqlite');found=[]
    if not path.exists():return None
    db=read_connection(path)
    try:
        for r in db.execute("SELECT event_key,local_receive_timestamp,payload FROM raw_events WHERE event_type='market_resolved' ORDER BY local_receive_timestamp"):
            raw=json.loads(r['payload'])
            if str(raw.get('id'))!=str(sample['market']['market_id']):continue
            v=terminal_value(account,sample['market'],raw,r['local_receive_timestamp'])
            found.append({**v,'event_key':r['event_key'],'raw_sha256':hashlib.sha256(r['payload'].encode()).hexdigest()})
    finally:db.close()
    if len({v['winning_asset_id'] for v in found})>1:raise ValueError('Conflicting terminal resolutions')
    return found[0] if found else None


def lane(root, output, sample, model, config):
    root,output=Path(root),Path(output);directory=output/sample['session_id']/model
    directory.mkdir();p=BookPolicy(**config);source=PaperStore.readonly(sample['tape'])
    store=SingleWriterStore(directory/'paper.sqlite', shared_inputs=directory.parent/'shared-inputs.sqlite');started=time.perf_counter()
    blocked=[]
    def no_network(*a,**kw):
        blocked.append('network_attempt');raise RuntimeError('Offline replay network denied')
    try:
        with patch.object(socket.socket,'connect',no_network),patch.object(socket.socket,'connect_ex',no_network),patch.object(socket,'getaddrinfo',no_network):
            om=UnifiedPaperManager(store,p,sample['start_ms']);session=IndexedHistoricalSession(om,model)
            prices={};derived=hashlib.sha256();seeded=False;frames_count=valid_count=0;unique=set();peak=D(0);last_frames={}
            with om.db:om.db.execute("INSERT INTO ip_session_config VALUES('rotation_origin',?)",(encode({'sample':sample,'profile':asdict(p),'split_shares':'5','not_original_execution':True}),))
            for e in observed_events(source,sample['tape_id']):
                current=redecode(e,True)
                if not seeded and current.get('kind')!='WATCHDOG' and len(current['frames'])==2 and not om.halted:
                    if all(not book_data_risk(f,p,e['time_ms']) and permission(f,p,e['time_ms'])[0]=='ACTIVE_MM' for f in current['frames']):
                        om.advance(e['time_ms']);om.split(current['frames'][0]['market'],'5','rotation-collateral-funded-seed');seeded=True
                if not seeded:
                    for f in current['frames']:
                        f['runtime_permission']={**f.get('runtime_permission',{}),'quotes_enabled':False}
                        f['rotation_seed_status']='WAITING_FOR_TWO_VALID_INPLAY_BOOKS'
                derived.update(encode(current).encode())
                session.on_event(current)
                for f in current['frames']:
                    frames_count+=1;point=price_point(f,p);prices.setdefault(f['asset_id'],[]).append(point)
                    if point['mid'] is not None:valid_count+=1;unique.add((f['asset_id'],f.get('book_ref')))
                    last_frames[f['asset_id']]=f
                q=list(om.account()['positions'].values())
                if len(q)==2:peak=max(peak,abs(D(q[0])-D(q[1])))
            result=report(om);state=om.ledger.state();before=om.account()
            fills=[]
            for r in om.db.execute("SELECT id,time_ms,payload FROM l6_events WHERE kind='FILL' ORDER BY seq"):
                f=json.loads(r['payload'])
                if f.get('purpose')!='SIMULATED_COMPLETE_SET_SPLIT':fills.append({**f,'id':r['id'],'receipt_ms':r['time_ms'],'time_ms':f.get('context',{}).get('exchange_ms') or r['time_ms']})
            fill_marks=[m for a,ps in prices.items() for m in markouts([f for f in fills if f['asset_id']==a],ps,sample['end_ms'],(10,30,60,300))]
            quotes=[{'id':o['id'],'time_ms':o['submit_ms'],'asset_id':o['asset_id'],'side':o['side'],'price':o['price'],'quantity':o['size']} for o in om.orders() if o.get('submit_ms') is not None]
            quote_marks=[{**m,'kind':'QUOTE_NOT_FILL'} for a,ps in prices.items() for m in markouts([f for f in quotes if f['asset_id']==a],ps,sample['end_ms'],(10,30,60,300))]
            initial_ids={l['id'] for l in state['lots'] if l.get('purpose')=='SIMULATED_COMPLETE_SET_SPLIT'}
            # Lots carry purpose as context? Explicit seed event IDs are the authoritative classification.
            initial_ids.update(r[0] for r in om.db.execute("SELECT id FROM l6_events WHERE kind='FILL' AND json_extract(payload,'$.purpose')='SIMULATED_COMPLETE_SET_SPLIT'"))
            closures=state['closures'];cycles=[c for c in closures if c['lot_id'] not in initial_ids]
            om.kill('rotation_end_cancel_not_liquidate');om.advance(om.now+max(p.cancel_latency_ms,p.request_timeout_ms)+1);om.reconcile()
            if not om.consistent() or om.account()['cash']!=before['cash'] or om.account()['positions']!=before['positions']:raise ValueError('Shutdown changed cash/inventory or failed reconciliation')
            terminal=resolution(root,sample,result['account'])
            if terminal:
                winner=terminal['winning_asset_id'];marks={o['asset_id']:('1' if o['asset_id']==winner else '0') for o in sample['market']['outcomes']}
                fifo=valuation(om.ledger.state(),marks)
                if D(fifo['net_pnl'])!=D(terminal['simulated_terminal_pnl']):raise ValueError('Terminal cash/FIFO mismatch')
            if om.real_submission_calls or om.adapter.real_submission_calls or blocked:raise ValueError('Non-Paper activity')
            result.update(profile='INPLAY_ROTATION_V1',mode='INDEPENDENT_POSTHOC_PAPER_NOT_REAL_PROFIT',fill_model=model,
                source_tape_id=sample['tape_id'],source_input_sha256=sample['source_input_sha256'],derived_input_sha256=derived.hexdigest(),
                session_id=sample['session_id'],question=sample['market'].get('question'),initial_split_shares='5' if seeded else '0',
                initial_split_cost_basis='0.5 each is bookkeeping allocation, not market value',
                terminal_valuation=terminal,shutdown_ledger_consistent=True,peak_net_directional_shares=str(peak),
                cycle_closures_excluding_seed=len(cycles),cycle_closed_quantity=str(sum((D(c['quantity']) for c in cycles),D(0))),
                cycle_realized_gross_pnl=str(sum((D(c['gross_pnl']) for c in cycles if c['gross_pnl'] is not None),D(0))),
                frame_coverage={'valid':valid_count,'total':frames_count,'unique_book_refs':len(unique),'not_full_game_certification':True},
                network_attempts=len(blocked),wall_seconds=time.perf_counter()-started,
                inventory_rule='SYMMETRIC_NET_SKEW; Q4/OT REDUCE_ONLY; 5 shares cash-funded split',
                phase_authority='observed period; precise countdown not used',
                cost_assumptions={'simulated_maker_fees':'0','rebates':'0','rewards':'0','gas':'0','basis':'research assumption; no actual cash receipts'},
                limitations=['Uncalibrated fill hypotheses; public aggressor side assumed','Captured depth is truncated; queue order and cancellations unknown',
                    'Seed-lot realized gains are not spread cycles; paired initial total cost is $1',
                    'Terminal winner valuation is posthoc accounting only; no execution/actual redemption',
                    'No external odds/true fair-value edge; no fees/rewards used to rescue performance',
                    'Same known samples used for strategy design; no out-of-sample claim; residual inventory can remain'])
            # Avoid copying redundant high-volume diagnostics into JSON; all remain in SQLite.
            for k in ('public_flow_diagnostics','own_fill_diagnostics'):result.pop(k,None)
            for name,value in [('result',result),('orders',om.orders()),('fills',fills),('fill-markouts',fill_marks),('quote-markouts',quote_marks),('closures',closures)]:dump(directory/(name+'.json'),value)
            return {'session_id':sample['session_id'],'model':model,'question':result['question'],'orders':result['orders'],
                    'fills':result['fill_events'],'cycles':len(cycles),'terminal_pnl':terminal['simulated_terminal_pnl'] if terminal else None,
                    'risk_state':result['risk_state'],'ledger_consistent':True,'result':str((directory/'result.json').relative_to(output))}
    finally:store.close();source.close()


def publish(output,manifest,rows,errors):
    dump(output/'summary.json',{'manifest':'manifest.json','rows':rows,'errors':errors,'live_go':False,'real_submission_calls':0})
    if rows:
        with (output/'comparison.csv').open('w',encoding='utf-8-sig',newline='') as fp:
            w=csv.DictWriter(fp,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
    body=['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>独立盘中 Paper 回放</title><link rel="stylesheet" href="/day-study.css"><body><main>',
          '<h1>盘中库存轮转 · 2¢ 主参数</h1><p>独立事后 Paper；不是真实成交或利润。每场每模型25虚拟美元，首次有效Q1–Q3用5美元模拟拆成两侧各5份；基础距中间价2¢，波动扩宽、库存倾斜，第四节/加时只减仓。三模型独立账户，不能相加为组合利润。<br>终值PnL＝模拟现金＋记录胜方剩余份额×1−本金；未捕获结算显示未知。初始份额的卖出收益不能直接称为价差。</p>',
          '<p>已完成 '+str(len(rows))+' 个模型分支；失败 '+str(len(errors))+'。原始数据和旧实验保留。<a href="comparison.csv">CSV</a> · <a href="manifest.json">样本与参数</a></p>',
          '<table><tr><th>样本 / 比赛</th><th>模型</th><th>订单 / 模拟成交</th><th>非初始库存闭合批次</th><th>假设结算PnL ($)</th><th>结束状态</th></tr>']
    by_session={s['session_id']:s for s in manifest['samples']}
    from datetime import datetime, timezone, timedelta
    tz=timezone(timedelta(hours=8))
    for r in sorted(rows,key=lambda r:(r['question'] or '',r['session_id'],r['model'])):
        sample=by_session[r['session_id']]
        label='赛前试采' if sample['period']=='PRE_GAME' else '盘中记录'
        window=datetime.fromtimestamp(sample['start_ms']/1000,tz).strftime('%m-%d %H:%M')+'–'+datetime.fromtimestamp(sample['end_ms']/1000,tz).strftime('%H:%M')
        body.append('<tr><td>'+html.escape(str(r['question']))+'<br><small>'+label+' · '+window+' UTC+8<br>'+r['session_id'][:8]+'</small></td><td><a href="'+r['result']+'">'+r['model']+'</a></td><td>'+str(r['orders'])+' / '+str(r['fills'])+'</td><td>'+str(r['cycles'])+'</td><td>'+str(r['terminal_pnl'] if r['terminal_pnl'] is not None else '未知')+'</td><td>'+r['risk_state']+'</td></tr>')
    body.append('</table><h2>边界</h2><p>盘口中间价不是公允概率；成交模型未校准；队列、真实延迟、奖励和费用资格不能从本结果推断。赛前试采片段应零报价，无有效盘口样本见manifest排除清单。行情/风控停机保留，不自动重启账户。周期批次和markout是诊断，不能再加到总PnL。</p>')
    if errors:body.append('<pre>'+html.escape(json.dumps(errors,ensure_ascii=False,indent=2))+'</pre>')
    (output/'index.html').write_text('\n'.join(body+['</main></body></html>']),encoding='utf-8')


def run(root,output,workers=3,distance='.02',resume=False):
    root,output=Path(root).resolve(),Path(output).resolve()
    manifest=json.loads((output/'manifest.json').read_text(encoding='utf-8')) if resume else prepare(root,output,distance)
    if manifest['profile']!=asdict(profile(distance)):raise ValueError('Immutable campaign parameters')
    rows=[];errors=[];jobs=[]
    for sample in manifest['samples']:
        for model in ('L0','L1','L2'):
            target=output/sample['session_id']/model
            if resume and (target/'result.json').exists():
                r=json.loads((target/'result.json').read_text(encoding='utf-8'));t=r['terminal_valuation']
                if r['policy']!=manifest['profile'] or r['source_tape_id']!=sample['tape_id'] or not r['shutdown_ledger_consistent']:raise ValueError('Saved lane mismatch')
                rows.append({'session_id':sample['session_id'],'model':model,'question':r['question'],'orders':r['orders'],'fills':r['fill_events'],'cycles':r['cycle_closures_excluding_seed'],'terminal_pnl':t['simulated_terminal_pnl'] if t else None,'risk_state':r['risk_state'],'ledger_consistent':True,'result':str((target/'result.json').relative_to(output))});continue
            if target.exists():raise ValueError('Incomplete lane preserved; use new campaign or investigate: '+str(target))
            jobs.append((root,output,sample,model,manifest['profile']))
    with ProcessPoolExecutor(max_workers=workers) as pool:
        pending={pool.submit(lane,*j):(j[2]['session_id'],j[3]) for j in jobs}
        for future in as_completed(pending):
            sid,model=pending[future]
            try:
                row=future.result();rows.append(row);print('COMPLETE '+json.dumps(row,ensure_ascii=False),flush=True)
            except Exception as e:
                import traceback
                errors.append({'session_id':sid,'model':model,'error':str(e),'traceback':traceback.format_exc()});print('FAILED '+sid+' '+model+' '+str(e),flush=True)
            publish(output,manifest,rows,errors)
    if protected_files(root)!=json.loads((output/'old-results-sha256.json').read_text(encoding='utf-8')):raise ValueError('Old results changed')
    for s in manifest['samples']:
        db=read_connection(root/'data'/s['path'])
        try:
            actual={'orders':db.execute('SELECT count(*) FROM pe_orders').fetchone()[0],'fills':db.execute('SELECT count(*) FROM pe_fills').fetchone()[0]}
            if actual!=s['original_counts']:raise ValueError('Original account changed')
        finally:db.close()
    dump(output/'audit.json',{'completed_lanes':len(rows),'expected_lanes':3*len(manifest['samples']),'failed_lanes':len(errors),'old_results_unchanged':True,'original_account_counts_unchanged':True,'live_go':False,'real_submission_calls':0})
    if errors:raise RuntimeError('Campaign has failed lanes; see summary.json')
    from ..storage_workflow import after_research
    after_research(root, output)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True);p.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[2]);p.add_argument('--workers',type=int,default=3);p.add_argument('--distance',default='.02');p.add_argument('--resume',action='store_true')
    a=p.parse_args()
    if not 1<=a.workers<=4:raise ValueError('1..4 bounded offline workers')
    run(a.root,a.out,a.workers,a.distance,a.resume)
