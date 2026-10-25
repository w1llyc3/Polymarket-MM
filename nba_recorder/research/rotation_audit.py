"""Independent read-only audit and aggregate diagnostics for a rotation campaign."""
import argparse
import csv
import hashlib
import json
from collections import Counter
from decimal import Decimal as D
from pathlib import Path
from ..fair_value.store import read_connection
from ..fill_management.ledger import Ledger, valuation, balance
from .rotation_study import dump, publish


def resting_time(db, end_ms):
    """Union of accepted virtual order lifetimes, not quote-decision counts."""
    phases=list(db.execute("SELECT time_ms,json_extract(payload,'$.state') FROM ip_phase_log ORDER BY time_ms,id"))
    live=next((t for t,s in phases if s in {'ACTIVE_MM','REDUCE_ONLY'}),None)
    closed=next((t for t,s in phases if s=='MM_CLOSED'),end_ms)
    fills=dict(db.execute('SELECT order_id,max(time_ms) FROM pe_fills GROUP BY order_id'))
    spans=[]
    for oid,start,cancel,state in db.execute("SELECT id,json_extract(payload,'$.submit_ms'),json_extract(payload,'$.cancelled_ms'),json_extract(payload,'$.state') FROM pe_exchange"):
        if state=='REJECTED' or start is None or live is None:continue
        stop=min(x for x in (cancel,fills.get(oid) if state=='FILLED' else None,end_ms,closed) if x is not None)
        if stop>max(start,live):spans.append((max(start,live),stop))
    merged=[]
    for start,stop in sorted(spans):
        if merged and start<=merged[-1][1]:merged[-1][1]=max(stop,merged[-1][1])
        else:merged.append([start,stop])
    duration=sum(b-a for a,b in merged);observed=closed-live if live is not None and closed>live else None
    return {'paper_any_order_resting_ms':duration,'observed_live_window_ms':observed,
            'paper_resting_time_fraction':str(D(duration)/observed) if observed else None,
            'observed_live_start_ms':live,'observed_live_end_ms':closed if live else None}


def validate(output, partial=False):
    output=Path(output);manifest=json.loads((output/'manifest.json').read_text(encoding='utf-8'))
    summary=json.loads((output/'summary.json').read_text(encoding='utf-8'));rows=[];samples=[];failures=[]
    if summary['errors'] or (not partial and len(summary['rows'])!=len(manifest['samples'])*3):raise ValueError('Incomplete/failed campaign')
    for sample in manifest['samples']:
        if partial and any(not (output/sample['session_id']/m/'result.json').exists() for m in ('L0','L1','L2')):continue
        hashes=[];models=[]
        for model in ('L0','L1','L2'):
            folder=output/sample['session_id']/model;r=json.loads((folder/'result.json').read_text(encoding='utf-8'))
            if sample['period']=='PRE_GAME' and (r['orders'] or r['initial_split_shares']!='0'):raise ValueError('Unexpected strategy activation in pregame sample')
            hashes.append(r['derived_input_sha256']);db=read_connection(folder/'paper.sqlite')
            try:
                if db.execute('SELECT mode FROM pe_control').fetchone()[0]!='PAPER':raise ValueError('Non-Paper account')
                rid=db.execute("SELECT run_id FROM l6_accounts WHERE lane='UNIFIED_PAPER'").fetchone()[0]
                ledger=Ledger(db,rid,'UNIFIED_PAPER');ledger.audit();state=ledger.state()
                if r['real_submission_calls']!=0 or r['network_attempts']!=0:raise ValueError('Network/live activity')
                if D(state['cash'])!=D(r['account']['cash']):raise ValueError('Cash mismatch')
                if set(r['account']['positions'])-set(state['assets']) or any(balance(state,a)!=D(q) for a,q in r['account']['positions'].items()):raise ValueError('FIFO position mismatch')
                halt=db.execute("SELECT min(seq) FROM pe_journal WHERE kind='KILL_SWITCH'").fetchone()[0]
                late=db.execute("SELECT count(*) FROM pe_journal WHERE kind='SUBMIT_REQUEST' AND seq>?",(halt,)).fetchone()[0] if halt else 0
                if late:raise ValueError('Submission after HALT')
                orders=[json.loads(x[0]) for x in db.execute('SELECT payload FROM pe_orders')]
                if any(o['state'] in {'OPEN','PARTIALLY_FILLED','UNKNOWN','TIMEOUT','CANCEL_PENDING'} or (o['state']=='RECONCILED' and o.get('confirmed_state') not in {'FILLED','CANCELLED','REJECTED'}) for o in orders):raise ValueError('Unresolved post-shutdown order')
                for o in orders:
                    if not D(0)<=D(o['filled'])<=D(o['size']):raise ValueError('Fill size')
                    authority=db.execute('SELECT payload FROM pe_exchange WHERE id=?',(o['id'],)).fetchone()
                    if authority and D(json.loads(authority[0])['filled'])!=D(o['filled']):raise ValueError('OMS/authority cumulative fill mismatch')
                    if o.get('submit_ms') is None:continue
                    if o['maker_only'] is not True:raise ValueError('Non-maker order')
                    f=o['frame'];b=f['book'];price=D(o['price']);tick=D(b['tick_size'])
                    if price%tick or not tick<=price<=1-tick:raise ValueError('Price/tick')
                    if o['purpose']=='MAKER' and (f.get('sports') or {}).get('period',99)>=4:raise ValueError('Q4 new entry')
                    if f['time_ms']>o['submit_ms'] or f['book_received_ms']>o['submit_ms']:raise ValueError('Future order reference')
                terminal=r['terminal_valuation'];unrealized=None
                if terminal:
                    marks={o['asset_id']:('1' if o['asset_id']==terminal['winning_asset_id'] else '0') for o in sample['market']['outcomes']}
                    value=valuation(state,marks)
                    if D(value['net_pnl'])!=D(terminal['simulated_terminal_pnl']):raise ValueError('Equity mismatch')
                    unrealized=value['unrealized_trading_pnl']
                fills=json.loads((folder/'fills.json').read_text(encoding='utf-8'))
                if len(fills)!=r['fill_events'] or len({f['id'] for f in fills})!=len(fills):raise ValueError('Fill uniqueness/count')
                old_pnl=None
                if sample.get('old_result_folder') and terminal:
                    old=json.loads((Path(sample['old_result_folder'])/('REDECODED-'+model)/'result.json').read_text(encoding='utf-8'))['account']
                    old_pnl=str(D(old['cash'])+D(old['positions'].get(terminal['winning_asset_id'],'0'))-D(old['capital_contributions']))
                halt_detail=db.execute("SELECT time_ms,json_extract(payload,'$.detail.reason') FROM pe_journal WHERE kind='KILL_SWITCH' ORDER BY seq LIMIT 1").fetchone()
                row={'session_id':sample['session_id'],'market_id':sample['market']['market_id'],'question':sample['market'].get('question'),
                     'sample_period':sample['period'],'model':model,'source_start_ms':sample['start_ms'],'source_end_ms':sample['end_ms'],
                     'orders':r['orders'],'fills':r['fill_events'],'filled_order_rate':r['simulated_fill_order_rate'],
                     'buy_fills':sum(f['side']=='BUY' for f in fills),'sell_fills':sum(f['side']=='SELL' for f in fills),
                     'cycle_closures_excluding_seed':r['cycle_closures_excluding_seed'],'cycle_closed_quantity':r['cycle_closed_quantity'],
                     'cycle_realized_gross_pnl':r['cycle_realized_gross_pnl'],'realized_pnl_including_seed_basis':r['account']['realized_trading_pnl'],
                     'terminal_unrealized_pnl':unrealized,'terminal_pnl':terminal['simulated_terminal_pnl'] if terminal else None,
                     'old_strategy_terminal_pnl':old_pnl,'end_cash':r['account']['cash'],
                     'paired_shares_at_end':str(min((D(q) for q in r['account']['positions'].values()),default=D(0))),
                     'direct_reduction_sell_quantity':str(sum((D(f['quantity']) for f in fills if f['side']=='SELL' and f.get('purpose')=='RISK_EXIT'),D(0))),
                     'complement_reduction_buy_quantity':str(sum((D(f['quantity']) for f in fills if f['side']=='BUY' and f.get('purpose')=='RISK_EXIT'),D(0))),
                     'simulated_fees':r['account']['fees'],'simulated_rewards':r['account']['rewards'],'end_positions':json.dumps(r['account']['positions'],sort_keys=True),
                     'peak_net_directional_shares':r['peak_net_directional_shares'],'quote_decision_availability':r['quote_availability'],
                     'first_kill_ms':halt_detail[0] if halt_detail else None,'first_kill_reason':halt_detail[1] if halt_detail else None,
                     'partial_filled_orders':sum(D(0)<D(o['filled'])<D(o['size']) for o in orders),
                     'risk_state_at_input_end':r['risk_state'],'valid_book_frames':r['frame_coverage']['valid'],'total_frames':r['frame_coverage']['total'],
                     'unique_book_refs':r['frame_coverage']['unique_book_refs'],'real_submission_calls':0,'network_attempts':0,'ledger_verified':True}
                row.update(resting_time(db,sample['end_ms']))
                marks=json.loads((folder/'fill-markouts.json').read_text(encoding='utf-8'))
                for seconds in (10,30,60,300):
                    m=[v for v in marks if v['seconds']==seconds and v['status']=='VALID'];q=sum((D(v['quantity']) for v in m),D(0))
                    row[f'markout_{seconds}s_observed']=len(m)
                    row[f'markout_{seconds}s_weighted_cents']=str(sum((D(v['quantity'])*D(v['fill_markout_cents']) for v in m),D(0))/q) if q else None
                rows.append(row);models.append({'model':model,'ledger_verified':True,'submissions_after_halt':late,'fills_unique':True})
            finally:db.close()
        if len(set(hashes))!=1:raise ValueError('Models used different input events: '+sample['session_id'])
        samples.append({'session_id':sample['session_id'],'same_derived_input_sha256':hashes[0],'models':models})
    with (output/'metrics.csv').open('w',encoding='utf-8-sig',newline='') as fp:
        w=csv.DictWriter(fp,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
    totals=[]
    for model in ('L0','L1','L2'):
        selected=[r for r in rows if r['model']==model and r['sample_period']=='TIME_OVERLAPS_GAME_NOT_VERIFIED']
        known=[r for r in selected if r['terminal_pnl'] is not None]
        old=[r for r in selected if r['old_strategy_terminal_pnl'] is not None]
        totals.append({'model':model,'inplay_samples':len(selected),'terminal_valued_samples':len(known),'capital_per_sample':'25',
            'terminal_pnl':str(sum((D(r['terminal_pnl']) for r in known),D(0))),'old_strategy_pnl_same_samples':str(sum((D(r['old_strategy_terminal_pnl']) for r in old),D(0))),
            'fills':sum(r['fills'] for r in selected),'buy_fills':sum(r['buy_fills'] for r in selected),'sell_fills':sum(r['sell_fills'] for r in selected),
            'cycle_closures_excluding_seed':sum(r['cycle_closures_excluding_seed'] for r in selected),
            'cycle_realized_gross_pnl':str(sum((D(r['cycle_realized_gross_pnl']) for r in selected),D(0))),
            'halted_samples':sum(r['risk_state_at_input_end']=='HALTED' for r in selected),
            'paper_resting_time_fraction':str(D(sum(r['paper_any_order_resting_ms'] for r in selected))/sum(r['observed_live_window_ms'] or 0 for r in selected)) if any(r['observed_live_window_ms'] for r in selected) else None,
            'max_net_directional_shares':str(max((D(r['peak_net_directional_shares']) for r in selected),default=D(0)))})
    validation={'status':'PARTIAL_PASS' if partial else 'PASS','sample_count':len(samples),'lanes':len(rows),'samples':samples,'totals':totals,
        'excluded':manifest['excluded'],'real_submission_calls':0,'network_attempts':0,'live_go':False,
        'limitations':['Decision availability is event-weighted, not wall time','Risk halts retained; no automatic account reset',
            'No statistical independence across games or fill models; no holdout evaluation',
            'Net PnL under zero simulated costs and terminal outcome valuation; not actual redemption or profit']}
    dump(output/('partial-validation.json' if partial else 'validation.json'),validation)
    if partial:return validation
    # Re-render with current readonly report code; never touch old day-study reports.
    publish(output,manifest,summary['rows'],summary['errors'])
    html_path=output/'index.html';page=html_path.read_text(encoding='utf-8')
    intro='<h2>'+str(totals[0]['inplay_samples'])+'场盘中样本 · 模型分别汇总</h2><p><a href="metrics.csv">完整指标CSV</a> · <a href="validation.json">独立账本验证</a></p><ul>'
    for t in totals:
        intro+='<li>'+t['model']+'：假设结算PnL $'+t['terminal_pnl']+'；旧策略同样本 $'+t['old_strategy_pnl_same_samples']+'；模拟成交 '+str(t['fills'])+'；非初始库存闭合批次 '+str(t['cycle_closures_excluding_seed'])+'；停机样本 '+str(t['halted_samples'])+'</li>'
    intro+='</ul><p>盈亏依赖成交模型、剩余库存与赛果；不同模型禁止相加。新旧同时改变多项规则，不能把差额归因于单一参数。</p>'
    page=page.replace('<body>', '<body class="rotation-report">').replace('<table>',intro+'<div class="table-scroll"><table>',1).replace('</table>','</table></div>').replace('\\L0\\','/L0/').replace('\\L1\\','/L1/').replace('\\L2\\','/L2/')
    html_path.write_text(page,encoding='utf-8')
    return validation


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True);p.add_argument('--partial',action='store_true');a=p.parse_args()
    print(json.dumps(validate(a.out,a.partial)['totals'],ensure_ascii=False,indent=2))
