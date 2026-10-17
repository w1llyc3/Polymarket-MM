"""Deterministic as-of analysis. Read-only account access, no execution imports."""
import json
from bisect import bisect_right
from decimal import Decimal, ROUND_FLOOR
from ..fair_value.store import read_connection
from ..fill_management.exits import executable

D = Decimal


def distribution(trades, asset, start, end, bucket='.01', amount=False):
    step = D(bucket)
    if not step.is_finite() or not D('.001') <= step <= D('.1'):
        raise ValueError('bucket must be .001 to .1 dollars')
    bins, seen, excluded = {}, set(), 0
    for t in trades:
        if t.get('asset_id') != asset or not start <= t['time_ms'] <= end:
            continue
        identity = t.get('event_id') or t.get('id')
        if not identity or identity in seen:
            continue
        seen.add(identity)
        try:
            p, q = D(str(t['price'])), D(str(t.get('size', t.get('quantity'))))
            if not p.is_finite() or not q.is_finite() or not 0 < p < 1 or q <= 0:
                raise ValueError('invalid print')
        except (ValueError, ArithmeticError, KeyError):
            excluded += 1
            continue
        key = (p/step).to_integral_value(rounding=ROUND_FLOOR)*step
        bins[key] = bins.get(key, D(0))+(p*q if amount else q)
    peak = max(bins.values(), default=D(0))
    return {'bins':[{'price':str(p),'value':str(v),'peak':v==peak} for p,v in sorted(bins.items())],
            'total':str(sum(bins.values(), D(0))), 'excluded':excluded,'unique_events':len(seen),
            'unit':'dollars' if amount else 'shares','boundary':'floor(price / bucket); left inclusive', 'bucket':str(step)}


def markouts(fills, prices, as_of, horizons=(1,3,10,30)):
    prices=sorted(prices,key=lambda x:x['time_ms']); times=[p['time_ms'] for p in prices]
    def mid(at):
        i=bisect_right(times,at)-1
        p=prices[i] if i>=0 else None
        return D(p['mid']) if p and p.get('mid') is not None and at <= p['valid_until_ms'] else None
    out=[]
    for f in fills:
        if f['time_ms']>as_of:continue
        initial=mid(f['time_ms'])
        for h in horizons:
            target=f['time_ms']+h*1000
            later=mid(target) if target<=as_of else None
            sign=D(1) if f['side']=='BUY' else D(-1)
            out.append({'fill_id':f['id'],'seconds':h,'side':f['side'],'quantity':f['quantity'],
                        'status':'PENDING' if target>as_of else 'VALID' if later is not None else 'UNKNOWN',
                        'fill_markout_cents':str(100*sign*(later-D(f['price']))) if later is not None else None,
                        'post_fill_drift_cents':str(100*sign*(later-initial)) if later is not None and initial is not None else None})
    return out


def exit_depth(account, frames, at, max_age=3000, fee_rate=None):
    """One depth budget per asset across all lots. Pending sells remain reserved."""
    positions=account.get('positions',{}); reserved=account.get('reserved_inventory',{})
    result=[]
    for asset, qty in positions.items():
        qty=D(qty); frame=frames.get(asset)
        base={'asset_id':asset,'held':str(qty),'available':str(max(D(0),qty-D(reserved.get(asset,'0')))),
              'hold':{'future_value':None,'future_liquidity':'UNKNOWN'},'fee_assumption':fee_rate,'auto_execute':False}
        if not frame or not frame.get('connected') or not 0<=at-frame['book_received_ms']<=max_age or frame['book'].get('flags'):
            result.append({**base,'status':'MANUAL_REVIEW','reason':'missing_or_stale_book'});continue
        available=D(base['available']); b=frame['book']
        direct=executable(b['bids'],available,False,fee_rate)
        other=next((o['asset_id'] for o in frame['market']['outcomes'] if o['asset_id']!=asset),None)
        unpaired=max(D(0),qty-D(positions.get(other,'0'))-D(reserved.get(asset,'0')))
        hedge=None; opposite=frames.get(other)
        if opposite and opposite.get('connected') and 0<=at-opposite['book_received_ms']<=max_age and not opposite['book'].get('flags'):
            # Cap each candidate to free cash. Candidates are alternatives, not simultaneous promises.
            cash=D(account.get('available_cash',account.get('cash','0'))); levels=[]
            for level in opposite['book']['asks']:
                p=D(level['price']); q=min(D(level['size']),max(D(0),cash/(p+(D(fee_rate)*p*(1-p) if fee_rate is not None else D(0)))))
                levels.append({'price':str(p),'size':str(q)});cash-=q*(p+(D(fee_rate)*p*(1-p) if fee_rate is not None else D(0)))
            hedge=executable(levels,unpaired,True,fee_rate)
        result.append({**base,'status':'EXIT_CANDIDATE','direct':direct,'complement':hedge,
                       'net_direct_cash':str(D(direct['gross_cash'])-D(direct['fee'])) if direct['fee'] is not None else None,
                       'conditional_pair_value_only':True,'alternatives_not_additive':True})
    return result


def analyze(path, as_of=None):
    db=read_connection(path)
    try:
        db.execute('BEGIN')
        last=db.execute('SELECT max(time_ms),max(rowid) FROM ip_inputs').fetchone()
        cutoff=min(as_of,last[0]) if as_of is not None and last[0] is not None else last[0]
        if cutoff is None:return {'as_of_ms':None,'coverage':'NO_DATA'}
        frames,prices,trades,fills={}, {}, [], []
        input_cursor=0
        for row in db.execute('SELECT rowid,payload FROM ip_inputs WHERE time_ms<=? ORDER BY time_ms,rowid',(cutoff,)):
            e=json.loads(row[1]);input_cursor=max(input_cursor,row[0]);at=e['time_ms']
            for f in e['frames']:
                asset=f['asset_id'];frames[asset]=f;b=f.get('book') or {}
                good=f.get('connected') and not b.get('flags') and b.get('bid') is not None and b.get('ask') is not None and 0<=at-f['book_received_ms']<=3000
                prices.setdefault(asset,[]).append({'time_ms':at,'mid':str((D(b['bid'])+D(b['ask']))/2) if good else None,'valid_until_ms':f['book_received_ms']+3000})
            trades.extend({**t,'time_ms':at} for t in e.get('trades',[]))
        account={};valuation={};journal_cursor=0;pending={};accepted_ms=0;previous=None;last_time=None;cancel_reasons={}
        for row in db.execute('SELECT seq,time_ms,kind,payload FROM pe_journal WHERE time_ms<=? ORDER BY seq',(cutoff,)):
            seq,at,kind,raw=tuple(row);journal_cursor=seq;p=json.loads(raw);o=p.get('order');d=p.get('detail') or {}
            if not isinstance(d,dict):d={}
            if last_time is not None and pending:accepted_ms+=max(0,at-last_time)
            last_time=at
            control=p.get('control',{});a=control.get('account')
            if a:account=json.loads(a) if isinstance(a,str) else a
            if o:
                if o['state'] in {'QUEUED','SUBMITTING','OPEN','PARTIALLY_FILLED','CANCEL_PENDING','UNKNOWN','TIMEOUT','RECONCILED'}:pending[o['id']]=o
                elif o['state'] in {'CANCELLED','FILLED','REJECTED'}:pending.pop(o['id'],None)
                authority=d.get('authority')
                if authority and D(str(d.get('delta_fill','0')))>0:
                    fills.append({'id':d.get('report_id') or str(seq),'asset_id':o['asset_id'],'side':o['side'],'price':o['price'],'quantity':str(d['delta_fill']),'time_ms':at})
            if kind=='BOOK_PAPER_EQUITY':valuation=d.get('ledger_valuation') or {};previous=at
            if kind in {'CANCEL_REQUEST','EXPIRY_DETECTED'}:cancel_reasons[kind]=cancel_reasons.get(kind,0)+1
        from ..paper_execution.risk import commitments
        budget=commitments(account,list(pending.values())) if account else {}
        account['reserved_inventory']={k:str(v) for k,v in budget.get('reserved_sell',{}).items()}
        account['available_cash']=str(D(account.get('cash','0'))-budget.get('reserved_cash',D(0)))
        assets=list(frames);pos=account.get('positions',{})
        summaries={}
        for asset in assets:
            values=markouts([f for f in fills if f['asset_id']==asset],prices[asset],cutoff)
            weighted=[]
            for side in ('BUY','SELL'):
                for seconds in (1,3,10,30):
                    group=[v for v in values if v['side']==side and v['seconds']==seconds]
                    valid=[v for v in group if v['status']=='VALID'];qty=sum((D(v['quantity']) for v in valid),D(0))
                    weighted.append({'side':side,'seconds':seconds,'fills':len(group),'mature_valid':len(valid),'valid_shares':str(qty),
                                     'weighted_fill_markout_cents':str(sum((D(v['quantity'])*D(v['fill_markout_cents']) for v in valid),D(0))/qty) if qty else None})
            summaries[asset]={'weighted_markouts':weighted}
        return {'as_of_ms':cutoff,'input_cursor':input_cursor,'journal_cursor':journal_cursor,'version':'research-v1',
                'sources':{'public':'recorded public prints only','fills':'PAPER_EXECUTION'},'real_submission_calls':0,
                'account':account,'valuation':valuation,'valuation_as_of_ms':previous,'accepted_order_time_ms':accepted_ms,'cancel_reasons':cancel_reasons,
                'paired_shares':str(min((D(pos.get(a,'0')) for a in assets),default=D(0))),
                'direction_order':assets,'net_directional_shares':str(D(pos.get(assets[0],'0'))-D(pos.get(assets[1],'0'))) if len(assets)==2 else None,
                'exit_candidates':exit_depth(account,frames,cutoff),
                'assets':{a:{**summaries[a],'distribution':distribution(trades,a,0,cutoff),
                             'recent_distribution':distribution(trades,a,max(0,cutoff-120000),cutoff),
                             'previous_distribution':distribution(trades,a,max(0,cutoff-240000),max(0,cutoff-120001)),
                             'markouts':markouts([f for f in fills if f['asset_id']==a],prices[a],cutoff)} for a in assets},
                'sample_size':{'public_events':len(trades),'paper_fills':len(fills),'independent_games':1},
                'candidate_plan':'继续观察；方向情景需用户记录触发价与失效条件，不自动执行',
                'limitations':['模拟成交未校准','费用未知：仅毛退出现金可用','Markout独立诊断，不加到PnL','未证明完整比赛覆盖']}
    finally:db.close()
