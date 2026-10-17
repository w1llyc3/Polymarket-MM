"""Explicit bounded depth exit; same Paper OMS/FIFO, no live adapter."""
from ..fair_value.maths import decimal as D
from ..paper_execution.risk import ACTIVE
from ..storage import encode
from .reference import book_risk, reduction_capacity


def execute(om,frame,quantity,minimum_price,request_id,fee_rate='.02'):
    if not om.policy.research_phase or om.halted or book_risk(frame,om.policy,om.now):
        raise ValueError('active valid research phase/account required')
    q,floor,rate=D(quantity),D(minimum_price),D(fee_rate)
    if not 0<q<=D(om.policy.max_asset_inventory) or not 0<floor<1 or not 0<=rate<=1:
        raise ValueError('bounded quantity, minimum price and simulated fee required')
    if any(o['state'] in ACTIVE for o in om.orders()):
        for o in om.orders():
            if o['state'] in ACTIVE:om.cancel(o['id'])
        return {'status':'WAIT_CANCEL_CONFIRMATION','filled':'0','remaining':str(q)}
    q=min(q,reduction_capacity({'asset_id':frame['asset_id'],'side':'SELL'},frame,om.account(),[]))
    if q<=0:raise ValueError('no unpaired inventory to reduce')
    grant={'id':request_id,'asset_id':frame['asset_id'],'market_id':frame['market_id'],'quantity':str(q),
           'minimum_price':str(floor),'expires_ms':om.now+om.policy.ttl_ms,'fee_rate':str(rate)}
    key='paper_exit:'+request_id
    with om.db:
        if om.db.execute('SELECT 1 FROM ip_session_config WHERE id=?',(key,)).fetchone():raise ValueError('authorization already used')
        om.db.execute('INSERT INTO ip_session_config VALUES(?,?)',(key,encode(grant)))
        om._log('BOUNDED_PAPER_EXIT_AUTHORIZED',detail=grant)
    left=q;ids=[];fee=D(0);gross=D(0)
    for level in frame['book']['bids']:
        price,available=D(level['price']),D(level['size'])
        if price<floor:break
        while available>0 and left>0:
            take=min(left,available,D(om.policy.max_order_shares),D(om.policy.max_order_notional)/price)
            take=(take//D('.01'))*D('.01')
            if take<D(frame['book']['minimum_order_size']):break
            target={'side':'SELL','price':str(price),'size':str(take),'maker_only':False,'purpose':'BOUNDED_PAPER_EXIT',
                    'event_id':frame['market']['event_id'],'market_id':frame['market_id'],'asset_id':frame['asset_id'],
                    'exit_grant_id':request_id,'expires_ms':grant['expires_ms']}
            identity=om.enqueue(request_id+':'+str(len(ids)),target,frame);om.dispatch()
            if om.order(identity)['state']!='SUBMITTING':break
            if not om.adapter.fill(identity,str(take),om.now,request_id+':fill:'+str(len(ids))):break
            om.advance(om.now+om.policy.submit_ack_ms+om.policy.data_transport_ms+1)
            ids.append(identity);left-=take;available-=take;gross+=take*price;fee+=take*rate*price*(1-price)
        if left<=0:break
    if fee:
        om.income(request_id+':fee','SIMULATED_COST','RECEIVED',str(fee),{'observed_ms':om.now,'cash_flow_id':request_id+':fee','provenance':'explicit research fee assumption, not verified exchange fee'},frame['market_id'])
    result={'status':'PAPER_DEPTH_SIMULATED','filled':str(q-left),'remaining':str(left),'gross':str(gross),'simulated_fee':str(fee),'orders':ids,'real_submission_calls':0}
    with om.db:om._log('BOUNDED_PAPER_EXIT_RESULT',detail=result)
    return result
