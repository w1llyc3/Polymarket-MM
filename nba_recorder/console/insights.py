"""Shared deterministic diagnostics. Never used to grant order permission."""
from decimal import Decimal as D


def intervals_union(intervals):
    result=[]
    for start,end in sorted(intervals):
        if end<=start:continue
        if result and start<=result[-1][1]:result[-1][1]=max(result[-1][1],end)
        else:result.append([start,end])
    return result


def overlap_ms(left,right):
    a,b=intervals_union(left),intervals_union(right);i=j=total=0
    while i<len(a) and j<len(b):
        total+=max(0,min(a[i][1],b[j][1])-max(a[i][0],b[j][0]))
        if a[i][1]<=b[j][1]:i+=1
        else:j+=1
    return total


def intersection(left,right):
    return [(max(a,c),min(b,d)) for a,b in intervals_union(left) for c,d in intervals_union(right) if max(a,c)<min(b,d)]


def monitor(prices,fills,ledger,events,decisions,cutoff,policy):
    active={};accepted={};cancel_reasons={};cases=[];submit={};cancel={};latencies={'submit_to_accept':[],'cancel_to_confirmation':[]}
    growth={};previous_positions={};peak=D(0);unresolved={};termination=None;parameters=[];source_gaps=[]
    for e in events:
        at,kind,o,d=e['time_ms'],e['kind'],e.get('order'),e['detail']
        positions=e.get('positions') or {}
        total=sum((D(v) for v in positions.values()),D(0));peak=max(peak,total)
        if cutoff-120000<=at<=cutoff:
            for a,q in positions.items():
                growth.setdefault(a,[previous_positions.get(a,'0'),q]);growth[a][1]=q
        previous_positions=positions
        if kind=='RESEARCH_PARAMETER_VERSION':parameters.append({'time_ms':at,**d})
        if kind=='KILL_SWITCH':termination={'time_ms':at,'reason':d.get('reason')}
        if kind in {'RISK','KILL_SWITCH','REQUEST_TIMEOUT','UNKNOWN','RECONCILED','BOUNDED_PAPER_EXIT_RESULT','MANUAL_RESEARCH_PHASE'}:
            cases.append({'time_ms':at,'kind':kind,'order_id':o.get('id') if o else None,'journal_seq':e['seq'],'evidence':d})
        if kind=='RISK' and any(x in str(d.get('reason','')) for x in ('stale','disconnect','backlog')):source_gaps.append({'time_ms':at,'reason':d.get('reason'),'journal_seq':e['seq']})
        if not o:continue
        identity=o['id'];asset=o['asset_id'];state=o['state'];authority=d.get('authority') or {}
        if kind=='SUBMIT_REQUEST':submit[identity]=at
        if kind=='CANCEL_REQUEST':
            cancel.setdefault(identity,at)
            reason=d.get('reason') or 'LEGACY_UNCLASSIFIED'
            cancel_reasons[reason]=cancel_reasons.get(reason,0)+1
        if authority.get('state') in {'OPEN','PARTIALLY_FILLED'} and identity not in active:
            active[identity]=(asset,at)
            if identity in submit:latencies['submit_to_accept'].append(at-submit.pop(identity))
        if authority.get('state') in {'FILLED','CANCELLED','REJECTED'} or state in {'UNKNOWN','TIMEOUT','RECONCILED'}:
            if identity in active:
                a,begin=active.pop(identity);accepted.setdefault(a,[]).append((begin,at))
            if authority.get('state')=='CANCELLED' and identity in cancel:latencies['cancel_to_confirmation'].append(at-cancel.pop(identity))
        if state in {'UNKNOWN','TIMEOUT','SUBMITTING','CANCEL_PENDING','RECONCILED'}:unresolved[identity]=state
        else:unresolved.pop(identity,None)
    for a,begin in active.values():accepted.setdefault(a,[]).append((begin,cutoff))
    permission=[];begin=None
    for e in events:
        if e.get('risk_state') in {'ACTIVE','RUNNING'}:
            if begin is None:begin=e['time_ms']
        elif begin is not None:
            permission.append((begin,e['time_ms']));begin=None
    if begin is not None:permission.append((begin,cutoff))
    availability={}
    for asset in prices:
        ds=sorted([d for d in decisions if d.get('frame',{}).get('asset_id')==asset],key=lambda d:d['frame']['time_ms'])
        eligible=[]
        for i,d in enumerate(ds):
            f=d['frame'];begin=f['time_ms']
            if not d.get('targets') or d.get('reasons'):continue
            end=min(cutoff,ds[i+1]['frame']['time_ms'] if i+1<len(ds) else cutoff,f['book_received_ms']+policy.book_max_age_ms)
            eligible.append((begin,end))
        eligible=intersection(eligible,permission);den=sum(b-a for a,b in eligible);num=overlap_ms(eligible,accepted.get(asset,[]))
        availability[asset]={'eligible_decision_ms':den,'accepted_eligible_ms':num,'ratio':str(D(num)/D(den)) if den else None,
                             'basis':'time intersection of recorded feasible target intervals and confirmed accepted orders; no frame counting'}
    assets={}
    for asset,ps in prices.items():
        recent=[p for p in ps if p['time_ms']>=cutoff-120000];valid=[p for p in recent if p['mid'] is not None]
        own=[f for f in fills if f['asset_id']==asset]
        closures=[c for c in ledger['closures'] if c['asset_id']==asset]
        qty=sum((D(c['quantity']) for c in closures),D(0));gross=sum((D(c['gross_pnl']) for c in closures if c.get('gross_pnl') is not None),D(0))
        current=ps[-1] if ps else None
        fresh=current and current['mid'] is not None and cutoff<=current.get('valid_until_ms',-1)
        assets[asset]={'current_spread_cents':str(100*(D(current['ask'])-D(current['bid']))) if fresh else None,
                       'recent_inventory_growth_shares':str(D(growth.get(asset,['0','0'])[1])-D(growth.get(asset,['0','0'])[0])),
                       'completed_turnover_shares':str(qty),'completed_gross_pnl':str(gross),
                       'quantity_weighted_holding_ms':str(sum((D(c['quantity'])*(c['exit_ms']-c['entry_ms']) for c in closures),D(0))/qty) if qty else None,
                       'depth_change_shares':str(D(valid[-1]['depth_shares'])-D(valid[0]['depth_shares'])) if len(valid)>=2 else None,
                       'own_fill_count':len(own),'last_valid_bbo_ms':valid[-1]['time_ms'] if valid else None}
    latency_summary={}
    for name,values in latencies.items():
        values=sorted(values);latency_summary[name]={'count':len(values),'basis':'Paper local visible report times, not real exchange latency'}
        for label,pct in (('p50',.5),('p95',.95),('p99',.99)):
            latency_summary[name][label]=values[max(0,__import__('math').ceil(pct*len(values))-1)] if values else None
    case_total=len(cases)
    return {'availability':availability,'assets':assets,'cancel_reasons':cancel_reasons,'unresolved_orders':unresolved,
            'peak_gross_inventory_shares':str(peak),'recent_data_issues':source_gaps[-100:],'system_cases':cases if case_total<=100 else cases[:25]+cases[-75:],
            'system_case_count':case_total,'case_projection_complete':case_total<=100,'complete_case_source':'pe_journal, referenced by journal_seq',
            'data_issue_count':len(source_gaps),'data_issue_projection_complete':len(source_gaps)<=100,
            'termination':termination,'parameter_versions':parameters,'latency_summary_ms':latency_summary,
            'diagnostic_only':True,'attribution_not_causal':True}
