import csv
import json
from bisect import bisect_right
from ..fair_value.maths import decimal
from ..inplay.reference import book_data_risk


def export_review(om, directory, report, cutoff):
    """Ordered event exports and retrospective-only quality diagnostics."""
    frames, count, valid = {}, 0, 0
    timeline = directory/'timeline.jsonl'
    peak = decimal(0)
    from ..inplay.observations import price_point
    with timeline.open('w',encoding='utf-8') as fp:
        for row in om.db.execute('SELECT rowid,time_ms,payload FROM ip_inputs ORDER BY time_ms,rowid'):
            event=json.loads(row['payload'])
            for f in event['frames']:
                count += 1
                if not book_data_risk({**f,'phase':'ACTIVE_MM'},om.policy,f['time_ms']):
                    valid += 1
                frames.setdefault(f['asset_id'],[]).append(price_point(f,om.policy))
            fp.write(json.dumps({'kind':'INPUT','ref':row['rowid'],**event})+'\n')
        for row in om.db.execute('SELECT seq,time_ms,kind,order_id,payload FROM pe_journal ORDER BY seq'):
            value=dict(row); value['payload']=json.loads(value['payload'])
            account=value['payload'].get('control',{}).get('account')
            if account:
                a=json.loads(account) if isinstance(account,str) else account
                peak=max(peak,sum((abs(decimal(q)) for q in a.get('positions',{}).values()),decimal(0)))
            fp.write(json.dumps({'kind':'ORDER_JOURNAL','journal':value})+'\n')
        for row in om.db.execute('SELECT id,market_id,time_ms,payload FROM ip_phase_log ORDER BY id'):
            fp.write(json.dumps({'kind':'PHASE_LOG','ref':row['id'],'market_id':row['market_id'],'time_ms':row['time_ms'],'phase':json.loads(row['payload'])})+'\n')
    fills=[]
    for row in om.db.execute("SELECT seq,id,time_ms,payload FROM l6_events WHERE kind='FILL' ORDER BY seq"):
        fill=json.loads(row['payload'])
        if fill.get('purpose')=='SIMULATED_COMPLETE_SET_SPLIT':continue
        fills.append({**fill,'id':row['id'],'time_ms':row['time_ms'],'ledger_seq':row['seq']})
    markouts=[]
    frame_times={asset:[f['time_ms'] for f in rows] for asset,rows in frames.items()}
    def reference_at(asset,at):
        rows=frames.get(asset,[]);i=bisect_right(frame_times.get(asset,[]),at)-1
        return rows[i] if i>=0 else None
    for fill in fills:
        at=fill.get('context',{}).get('exchange_ms',fill['time_ms'])
        for horizon in (10,30,60,300):
            target=at+horizon*1000
            # As-of target: choose last eligible visible input <= target; no forward interpolation.
            reference=reference_at(fill['asset_id'],target)
            status='PENDING' if target>cutoff else 'UNKNOWN'
            value=None
            if target<=cutoff and reference and reference['mid'] is not None and target<=reference['valid_until_ms']:
                mid=decimal(reference['mid'])
                value=str((mid-decimal(fill['price']))*(1 if fill['side']=='BUY' else -1))
                status='OBSERVED'
            markouts.append({'fill_id':fill['id'],'horizon_s':horizon,'status':status,
                'value_per_share':value,'quantity':fill.get('quantity'), 'book_ref':reference.get('book_ref') if reference else None,
                'not_added_to_pnl':True})
    orders=om.orders()
    prints=[json.loads(r[0]) for r in om.db.execute('SELECT payload FROM ip_prints')]
    calibration=[]
    for order in orders:
        if order.get('submit_ms') is None:continue
        authority=om.adapter.authoritative(order['id']) or {}
        stop=min(cutoff,authority.get('cancelled_ms',cutoff))
        price=decimal(order['price']);matches=[]
        for trade in prints:
            if trade.get('asset_id')!=order['asset_id'] or not order['submit_ms']<trade['exchange_ms']<=stop:continue
            if (order['side']=='BUY' and trade['side']=='SELL' and decimal(trade['price'])<=price) or (order['side']=='SELL' and trade['side']=='BUY' and decimal(trade['price'])>=price):matches.append(trade)
        touch=[]
        for f in frames.get(order['asset_id'],[]):
            if not order['submit_ms']<f['time_ms']<=stop:continue
            if f['mid'] is None:continue
            top=decimal(f['ask' if order['side']=='BUY' else 'bid'])
            if (top<=price if order['side']=='BUY' else top>=price):touch.append(f['book_ref'])
        queue=om.db.execute('SELECT payload FROM ip_models WHERE order_id=?',(order['id'],)).fetchone()
        calibration.append({'order_id':order['id'],'asset_id':order['asset_id'],'side':order['side'],
            'touch_observation_count':len(set(touch)),'crossing_public_print_ids':[t['event_id'] for t in matches],
            'simulated_filled_quantity':order['filled'],'queue_hypothesis':json.loads(queue[0]) if queue else None,
            'confidence':'UNCALIBRATED','retrospective_only':True,
            'limitations':['A print may be eligible for several local orders; do not sum these lists as market volume',
                           'Touch and public aggressor side do not prove our order would execute']})
    (directory/'calibration.json').write_text(json.dumps(calibration,ensure_ascii=False,indent=2),encoding='utf-8')
    quote_markouts=[]
    for order in orders:
        if order.get('submit_ms') is None:continue
        at=order['submit_ms']
        for horizon in (10,30,60,300):
            target=at+horizon*1000
            reference=reference_at(order['asset_id'],target)
            status='PENDING' if target>cutoff else 'UNKNOWN'; value=None
            if target<=cutoff and reference and reference['mid'] is not None and target<=reference['valid_until_ms']:
                mid=decimal(reference['mid'])
                value=str((mid-decimal(order['price']))*(1 if order['side']=='BUY' else -1));status='OBSERVED'
            quote_markouts.append({'order_id':order['id'],'horizon_s':horizon,'status':status,'value_per_share':value,
                'kind':'QUOTE_NOT_FILL','not_added_to_pnl':True,'book_ref':reference.get('book_ref') if reference else None})
    for name, rows in [('orders',orders),('fills',fills),('markouts',markouts),('quote-markouts',quote_markouts)]:
        (directory/(name+'.json')).write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
    decisions=[{**json.loads(r['payload']),'decision_ref':r['id']} for r in om.db.execute('SELECT id,payload FROM pe_quotes ORDER BY time_ms,rowid')]
    with (directory/'decisions.jsonl').open('w',encoding='utf-8') as fp:
        for decision in decisions:fp.write(json.dumps(decision)+'\n')
    filled=[o for o in orders if decimal(o.get('filled','0'))>0]
    partial=[o for o in filled if decimal(o['filled'])<decimal(o['size'])]
    from ..console.research import analyze
    # The shared analyzer also emits exit capacities, inventory lots, time-based opportunity availability.
    analysis=analyze(Path_of_db(om),cutoff)
    (directory/'analysis.json').write_text(json.dumps(analysis,ensure_ascii=False,indent=2),encoding='utf-8')
    result={'account':report['account'],'fill_events':len(fills),'orders':len(orders),
        'filled_order_rate':str(decimal(len(filled))/len(orders)) if orders else None,
        'partial_filled_order_rate':str(decimal(len(partial))/len(filled)) if filled else None,
        'quote_availability':analysis['monitoring']['availability'],
        'peak_inventory_shares':str(peak),'markouts':markouts,
        'frame_quality_valid':valid,'frame_count':count,'coverage_basis':'input frames, including reuse; not full-game coverage',
        'pnl':report['pnl_layers'],'outcome_fair_edge':None,'hedge_cost':None,
        'stale_quote_loss':report['stale_quote_loss_realized_diagnostic'],
        'latency_ms':report['latency_ms'],'latency_basis':'Paper virtual clock, not live latency',
        'attribution_overlap_not_summed':True,'real_submission_calls':0}
    result['calibration']={'orders':len(calibration),'touch_candidates':sum(c['touch_observation_count']>0 for c in calibration),
        'print_candidates':sum(bool(c['crossing_public_print_ids']) for c in calibration),
        'confidence':'UNCALIBRATED','detail_export':'calibration.json','real_fill_probability':None}
    result['markout_summary']={}
    for horizon in (10,30,60,300):
        rows=[m for m in markouts if m['horizon_s']==horizon]
        observed=[m for m in rows if m['status']=='OBSERVED']
        quantity=sum((decimal(m['quantity']) for m in observed),decimal(0))
        result['markout_summary'][str(horizon)]={'observed':len(observed),'pending':sum(m['status']=='PENDING' for m in rows),
            'unknown':sum(m['status']=='UNKNOWN' for m in rows),'quantity':str(quantity),
            'quantity_weighted_per_share':str(sum((decimal(m['value_per_share'])*decimal(m['quantity']) for m in observed),decimal(0))/quantity) if quantity else None}
    (directory/'summary.md').write_text(
        '# Offline Paper research\n\nSimulated results, not real profit.\n\n'
        +json.dumps(result,ensure_ascii=False,indent=2)+'\n\n'
        +'Detailed references: timeline.jsonl / orders.json / fills.json / markouts.json / decisions.jsonl / analysis.json.\n',encoding='utf-8')
    return result


def Path_of_db(om):
    from pathlib import Path
    return Path(om.db.execute('PRAGMA database_list').fetchone()[2])
