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
        if not frame or not frame.get('connected') or not 0<=at-frame['book_received_ms']<=max_age or frame['book'].get('flags') or frame['market']['status'].get('closed'):
            result.append({**base,'status':'MANUAL_REVIEW','reason':'missing_or_stale_book'});continue
        available=D(base['available']); b=frame['book']
        direct=executable(b['bids'],available,False,fee_rate)
        scenarios=[{'fee_rate':str(rate),'net_cash':str(D(executable(b['bids'],available,False,str(rate))['gross_cash'])-D(executable(b['bids'],available,False,str(rate))['fee'])),'basis':'research_assumption'} for rate in (D(0),D('.02'),D('.05'))]
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
                       'fee_sensitivity':scenarios,
                       'net_direct_cash':str(D(direct['gross_cash'])-D(direct['fee'])) if direct['fee'] is not None else None,
                       'conditional_pair_value_only':True,'alternatives_not_additive':True})
    return result


def analyze(path, as_of=None):
    if as_of is not None and (type(as_of) is not int or as_of<0):raise ValueError('nonnegative integer cutoff required')
    db=read_connection(path)
    try:
        db.execute('BEGIN')
        last=db.execute('SELECT max(time_ms),max(rowid) FROM ip_inputs').fetchone()
        journal_end=db.execute('SELECT max(time_ms) FROM pe_journal').fetchone()[0]
        watermark=max((v for v in (last[0],journal_end) if v is not None),default=None)
        cutoff=min(as_of,watermark) if as_of is not None and watermark is not None else watermark
        if cutoff is None:return {'as_of_ms':None,'coverage':'NO_DATA'}
        frames,prices,trades,fills={}, {}, [], []
        from ..inplay.policy import BookPolicy
        from ..inplay.reference import book_data_risk
        historical_control=db.execute('SELECT payload FROM pe_journal WHERE time_ms<=? ORDER BY seq DESC LIMIT 1',(cutoff,)).fetchone()
        if not historical_control:return {'as_of_ms':cutoff,'coverage':'NO_DATA'}
        policy=BookPolicy(**json.loads(json.loads(historical_control[0])['control']['config']))
        input_cursor=0
        for row in db.execute('SELECT rowid,payload FROM ip_inputs WHERE time_ms<=? ORDER BY time_ms,rowid',(cutoff,)):
            e=json.loads(row[1]);input_cursor=max(input_cursor,row[0]);at=e['time_ms']
            for f in e['frames']:
                asset=f['asset_id'];frames[asset]=f;b=f.get('book') or {}
                good=not book_data_risk(f,policy,at)
                prices.setdefault(asset,[]).append({'time_ms':at,'mid':str((D(b['bid'])+D(b['ask']))/2) if good else None,
                    'bid':b.get('bid'),'ask':b.get('ask'),'depth_shares':str(sum((D(l['size']) for side in ('bids','asks') for l in b.get(side,[])),D(0))),
                    'valid_until_ms':f['book_received_ms']+policy.book_max_age_ms})
            trades.extend({**t,'time_ms':at} for t in e.get('trades',[]))
        account={};valuation={};journal_cursor=0;pending={};resting=set();accepted_ms=0;previous=None;last_time=None;cancel_reasons={};segments=[];events=[]
        for row in db.execute('SELECT seq,time_ms,kind,payload FROM pe_journal WHERE time_ms<=? ORDER BY seq',(cutoff,)):
            seq,at,kind,raw=tuple(row);journal_cursor=seq;p=json.loads(raw);o=p.get('order');d=p.get('detail') or {}
            if not isinstance(d,dict):d={}
            if kind=='EXECUTION_SEGMENT_START':segments.append({'time_ms':at,**d})
            if last_time is not None and resting:accepted_ms+=max(0,at-last_time)
            last_time=at
            control=p.get('control',{});a=control.get('account')
            if a:account=json.loads(a) if isinstance(a,str) else a
            events.append({'seq':seq,'time_ms':at,'kind':kind,'order':{k:o[k] for k in ('id','asset_id','state')} if o else None,
                           'detail':{k:v for k,v in d.items() if k not in {'evidence','frame','ledger_valuation','before','after'}},
                           'risk_state':control.get('state'),'positions':account.get('positions',{})})
            if o:
                if o['state'] in {'QUEUED','SUBMITTING','OPEN','PARTIALLY_FILLED','CANCEL_PENDING','UNKNOWN','TIMEOUT','RECONCILED'}:pending[o['id']]=o
                elif o['state'] in {'CANCELLED','FILLED','REJECTED'}:pending.pop(o['id'],None)
                authority=d.get('authority')
                if authority and authority.get('state') in {'OPEN','PARTIALLY_FILLED'}:resting.add(o['id'])
                if authority and authority.get('state') in {'FILLED','CANCELLED','REJECTED'}:resting.discard(o['id'])
                if o['state'] in {'UNKNOWN','TIMEOUT','RECONCILED'}:resting.discard(o['id'])
                if authority and D(str(d.get('delta_fill','0')))>0:
                    fills.append({'id':d.get('report_id') or str(seq),'asset_id':o['asset_id'],'side':o['side'],'price':o['price'],'quantity':str(d['delta_fill']),'time_ms':at})
            if kind=='BOOK_PAPER_EQUITY':valuation=d.get('ledger_valuation') or {};previous=at
            if kind in {'CANCEL_REQUEST','EXPIRY_DETECTED'}:cancel_reasons[kind]=cancel_reasons.get(kind,0)+1
        from ..paper_execution.risk import commitments
        if last_time is not None and resting:accepted_ms+=max(0,cutoff-last_time)
        budget=commitments(account,list(pending.values())) if account else {}
        account['reserved_inventory']={k:str(v) for k,v in budget.get('reserved_sell',{}).items()}
        account['available_cash']=str(D(account.get('cash','0'))-budget.get('reserved_cash',D(0)))
        assets=list(frames);pos=account.get('positions',{})
        eligible=retained=0;decisions=[]
        for r in db.execute('SELECT payload FROM pe_quotes WHERE time_ms<=?',(cutoff,)):
            decision=json.loads(r[0]);f=decision.get('frame') or {}
            decisions.append({'frame':{k:f.get(k) for k in ('asset_id','time_ms','book_received_ms')},'targets':bool(decision.get('targets')),'reasons':decision.get('reasons')})
            retention=decision.get('retention',{});eligible+=retention.get('eligible_before',0);retained+=retention.get('retained',0)
        from ..fill_management.ledger import empty, reduce_event, valuation as ledger_valuation
        ledger=empty();ledger_cursor=0
        for r in db.execute("SELECT seq,id,time_ms,kind,payload FROM l6_events WHERE lane='UNIFIED_PAPER' AND time_ms<=? ORDER BY seq",(cutoff,)):
            ledger=reduce_event(ledger,{'id':r['id'],'time_ms':r['time_ms'],'kind':r['kind'],'payload':json.loads(r['payload'])});ledger_cursor=r['seq']
        marks={a:ps[-1]['mid'] for a,ps in prices.items() if ps and ps[-1]['mid'] is not None and cutoff<=ps[-1]['valid_until_ms']}
        valuation=ledger_valuation(ledger,marks,list(pending.values()));previous=cutoff
        from .insights import monitor
        monitoring=monitor(prices,fills,ledger,events,decisions,cutoff,policy)
        direction=None
        for e in events:
            if e['kind']=='DIRECTION_RESEARCH_PREPARATION':direction={'status':'WAIT_CANCEL_OR_RECONCILIATION','requested_ms':e['time_ms']}
            if e['kind']=='DIRECTION_RESEARCH_READY':direction={'status':'READY_FOR_MANUAL_DIRECTION_RESEARCH','ready_ms':e['time_ms'],'residual_inventory':e['detail'].get('residual_inventory'),'auto_execute':False}
        latencies={}
        for r in db.execute('SELECT kind,duration_ms FROM pe_latency WHERE time_ms<=? ORDER BY duration_ms',(cutoff,)):
            latencies.setdefault(r[0],[]).append(r[1])
        latencies={k:{'count':len(v),**{label:v[min(len(v)-1,max(0,__import__('math').ceil(len(v)*q)-1))] for label,q in [('p50',.5),('p95',.95),('p99',.99)]},'basis':'synthetic_paper_clock_not_real_latency'} for k,v in latencies.items()}
        summaries={}
        first_time=db.execute('SELECT min(time_ms) FROM ip_inputs WHERE time_ms<=?',(cutoff,)).fetchone()[0]
        duration=max(0,cutoff-first_time) if first_time is not None else 0
        coverage={}
        for a,ps in prices.items():
            ms=sum(max(0,min(ps[i+1]['time_ms'] if i+1<len(ps) else cutoff,p['valid_until_ms'],cutoff)-p['time_ms']) for i,p in enumerate(ps) if p['mid'] is not None)
            coverage[a]={'valid_bbo_ms':ms,'recorded_window_ms':duration,'ratio':str(D(ms)/D(duration)) if duration else None,'basis':'recorded interval only, not full game'}
        for asset in assets:
            values=markouts([f for f in fills if f['asset_id']==asset],prices[asset],cutoff)
            weighted=[]
            for side in ('BUY','SELL'):
                for seconds in (1,3,10,30):
                    group=[v for v in values if v['side']==side and v['seconds']==seconds]
                    valid=[v for v in group if v['status']=='VALID'];qty=sum((D(v['quantity']) for v in valid),D(0))
                    weighted.append({'side':side,'seconds':seconds,'fills':len(group),'mature_valid':len(valid),'valid_shares':str(qty),
                                     'pending':sum(v['status']=='PENDING' for v in group),'unknown':sum(v['status']=='UNKNOWN' for v in group),
                                     'weighted_fill_markout_cents':str(sum((D(v['quantity'])*D(v['fill_markout_cents']) for v in valid),D(0))/qty) if qty else None})
            recent=[p for p in prices[asset] if p['time_ms']>=cutoff-120000 and p['mid'] is not None]
            mids=[D(p['mid']) for p in recent]
            structure={'samples':len(mids),'window_ms':120000,'classification':'INSUFFICIENT_DATA'}
            if len(mids)>=5:
                delta=mids[-1]-mids[0]
                structure.update(low=str(min(mids)),high=str(max(mids)),change_cents=str(100*delta),
                                 classification='UP' if delta>=D('.02') else 'DOWN' if delta<=D('-.02') else 'RANGE',
                                 rule='2-cent end-to-start threshold; descriptive, not predictive',
                                 conditional_trigger_above=str(max(mids)),conditional_invalidation_below=str(min(mids)))
            summaries[asset]={'weighted_markouts':weighted,'price_structure':structure}
            current=frames[asset]
            if not book_data_risk(current,policy,cutoff):
                probe=D(policy.order_size)
                summaries[asset]['planned_liquidity']={'planned_quantity':str(probe),'buy':executable(current['book']['asks'],probe,True,None),
                    'sell':executable(current['book']['bids'],probe,False,None),'available_owned_shares':str(max(D(0),D(pos.get(asset,'0'))-D(account['reserved_inventory'].get(asset,'0')))),
                    'available_cash':account['available_cash'],'fee_status':'UNKNOWN','alternative_scenarios_not_joint_reservations':True}
            recent_dist=distribution(trades,asset,max(0,cutoff-120000),cutoff)
            previous_dist=distribution(trades,asset,max(0,cutoff-240000),max(0,cutoff-120001))
            def center(dist):
                total=sum((D(b['value']) for b in dist['bins']),D(0))
                return sum((D(b['price'])*D(b['value']) for b in dist['bins']),D(0))/total if total else None
            now_center,old_center=center(recent_dist),center(previous_dist)
            summaries[asset]['flow']={'public_prints_per_minute':str(D(recent_dist['unique_events'])/2),
                'distribution_migration_cents':str(100*(now_center-old_center)) if now_center is not None and old_center is not None else None,
                'recent_samples':recent_dist['unique_events'],'previous_samples':previous_dist['unique_events'],'window_ms':120000,
                'coverage_limited':coverage[asset]['ratio']!='1','identity_basis':'recorded public event IDs / fingerprint, not independent clearing volume'}
        return {'as_of_ms':cutoff,'input_cursor':input_cursor,'journal_cursor':journal_cursor,'version':'research-v1',
                'origin':json.loads((db.execute("SELECT payload FROM ip_session_config WHERE id='console_origin'").fetchone() or ['{}'])[0]),
                'bound_market':next(iter(frames.values()))['market'] if frames else None,
                'fill_model':(db.execute("SELECT payload FROM ip_session_config WHERE id='fill_model'").fetchone() or ['UNKNOWN'])[0],
                'session_completion':next((e['detail'] for e in reversed(events) if e['kind']=='CONSOLE_SESSION_FINISHED'),None),
                'bound_plan':next((e['detail'] for e in events if e['kind']=='RESEARCH_PLAN_BOUND'),None),
                'monitoring':monitoring,'direction_preparation':direction,'policy_as_of':__import__('dataclasses').asdict(policy),'accounting_closures':ledger['closures'],
                'sources':{'public':'recorded public prints only','fills':'PAPER_EXECUTION'},'real_submission_calls':0,
                'execution_segments':segments,
                'account':account,'valuation':valuation,'valuation_as_of_ms':previous,'ledger_cursor':ledger_cursor,
                'inventory_lots':[{'asset_id':l['asset_id'],'remaining':l['remaining'],'cost_per_share':l.get('price'),'age_ms':cutoff-l['time_ms']} for l in ledger['lots'] if D(l['remaining'])>0],
                'latency':latencies,'accepted_order_time_ms':accepted_ms,'cancel_reasons':cancel_reasons,
                'quote_availability':{'accepted_any_side_ms':accepted_ms,'recorded_window_ms':duration,'ratio':str(D(accepted_ms)/D(duration)) if duration else None,'basis':'known accepted order presence / recorded time; not eligibility-adjusted'},
                'data_coverage':coverage,
                'income_stages':[dict(r) for r in db.execute('SELECT id,time_ms,kind,stage,amount,market_id FROM ip_income WHERE time_ms<=? ORDER BY time_ms,id',(cutoff,))],
                'order_retention':{'eligible_before':eligible,'retained':retained,'ratio':str(D(retained)/D(eligible)) if eligible else None,'basis':'decision opportunities, not frames or time'},
                'paired_shares':str(min((D(pos.get(a,'0')) for a in assets),default=D(0))),
                'direction_order':assets,'net_directional_shares':str(D(pos.get(assets[0],'0'))-D(pos.get(assets[1],'0'))) if len(assets)==2 else None,
                'exit_candidates':exit_depth(account,frames,cutoff),
                'assets':{a:{**summaries[a],'distribution':distribution(trades,a,0,cutoff),
                             'recent_distribution':distribution(trades,a,max(0,cutoff-120000),cutoff),
                             'previous_distribution':distribution(trades,a,max(0,cutoff-240000),max(0,cutoff-120001)),
                             'markouts':markouts([f for f in fills if f['asset_id']==a],prices[a],cutoff)} for a in assets},
                'sample_size':{'public_events':len({t.get('event_id') for t in trades if t.get('event_id')}),'paper_fills':len(fills),'independent_games':1 if frames else 0},
                'candidate_plan':'继续观察；方向情景需用户记录触发价与失效条件，不自动执行',
                'limitations':['模拟成交未校准','费用未知：仅毛退出现金可用','Markout独立诊断，不加到PnL','未证明完整比赛覆盖']}
    finally:db.close()
