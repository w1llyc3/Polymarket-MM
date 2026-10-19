"""Pure offline mappings of official SDK/REST/user-stream shapes; no client or signer."""
from datetime import datetime
from ..fair_value.maths import decimal
from ..fair_value.store import digest


def timestamp_ms(value, numeric_unit='ms'):
    if value is None:return None
    if isinstance(value,datetime):
        if value.tzinfo is None:raise ValueError('Timezone required')
        return int(value.timestamp()*1000)
    if isinstance(value,str) and not value.isdigit():
        return timestamp_ms(datetime.fromisoformat(value.replace('Z','+00:00')))
    result=int(value)
    if numeric_unit not in {'ms','s'}:raise ValueError('Explicit timestamp unit required')
    return result*(1000 if numeric_unit=='s' else 1)


def normalize_order(raw, source='SDK', received_ms=None):
    if source not in {'SDK','CLOB_REST','USER_SDK','USER_RAW'}:raise ValueError('Explicit order source required')
    payload=raw.get('payload',raw)
    size=decimal(payload['original_size']);matched=decimal(payload['size_matched']);price=decimal(payload['price'])
    if size<=0 or not 0<=matched<=size or not 0<price<1 or payload['side'] not in {'BUY','SELL'}:
        raise ValueError('Invalid order quantities')
    status=str(payload.get('status') or '').upper()
    event_type=payload.get('order_event_type') if source=='USER_SDK' else payload.get('type') if source=='USER_RAW' else None
    if status in {'CANCELED','CANCELLED','CANCELED_MARKET_RESOLVED'} or event_type=='CANCELLATION':state='CANCELLED'
    elif status in {'LIVE','OPEN'}:state='PARTIALLY_FILLED' if matched else 'OPEN'
    elif status in {'MATCHED','FILLED'} and matched==size:state='FILLED'
    elif status in {'INVALID','REJECTED'} and not matched:state='REJECTED'
    else:state='UNKNOWN'
    return {'id':payload['id'],'asset_id':str(payload['asset_id']),
        'condition_id':payload.get('condition_id') if source in {'SDK','USER_SDK'} else payload.get('market'),
        'side':payload['side'],'price':str(price),'size':str(size),'filled':str(matched),
        'remaining':str(size-matched),'state':state,'exchange_status':status,
        'order_type':payload.get('order_type'),'received_ms':received_ms,
        'created_ms':timestamp_ms(payload.get('created_at'),'s' if source=='CLOB_REST' else 'ms'),
        'exchange_ms':timestamp_ms(payload.get('timestamp')) if source.startswith('USER') else None,
        'expires_ms':timestamp_ms(payload.get('expires_at')) if source in {'SDK','USER_SDK'} else timestamp_ms(payload.get('expiration'),'s') if payload.get('expiration') else None,
        'requires_reconciliation':state=='UNKNOWN','source':source,
        'quantity_units':'shares; never divide original_size/size_matched by 1e6'}


def normalize_cancel(raw, requested_ids):
    canceled=set(raw.get('canceled',[]));not_canceled=raw.get('not_canceled',{})
    return [{'order_id':identity,'state':'CANCELLED' if identity in canceled and identity not in not_canceled else 'UNKNOWN',
        'filled_quantity':None,'requires_fill_query':True,
        'reason':not_canceled.get(identity) if identity in not_canceled else None,
        'acknowledged':identity in canceled and identity not in not_canceled}
        for identity in requested_ids]


def normalize_trade(raw, owned_order_ids, received_ms=None, source='SDK'):
    p=raw.get('payload',raw);status=str(p.get('status','UNKNOWN')).removeprefix('TRADE_STATUS_')
    if source not in {'SDK','USER_SDK','CLOB_REST','USER_RAW'}:raise ValueError('Explicit trade source required')
    owned=set(owned_order_ids);role=p.get('trader_side');legs=[]
    condition=p.get('condition_id') if source in {'SDK','USER_SDK'} else p.get('market')
    if role=='TAKER' and p.get('taker_order_id') in owned:
        legs=[{'order_id':p['taker_order_id'],'asset_id':p['asset_id'],'side':p['side'],'price':p['price'],'matched_amount':p['size']}]
    elif role=='MAKER':
        legs=[m for m in p.get('maker_orders',[]) if m.get('order_id') in owned]
    result=[]
    for leg in legs:
        quantity=decimal(leg['matched_amount']);price=decimal(leg['price'])
        if quantity<=0 or not 0<price<1 or leg['side'] not in {'BUY','SELL'}:raise ValueError('Invalid own trade leg')
        result.append({'fill_id':digest(['external-trade-leg',p['id'],leg['order_id']]),'trade_id':p['id'],
            'order_id':leg['order_id'],'asset_id':str(leg['asset_id']),'condition_id':condition,
            'side':leg['side'],'quantity':str(quantity),'price':str(price),'role':role,
            'settlement_status':status,'confirmed':status=='CONFIRMED','failed':status=='FAILED',
            'exchange_ms':timestamp_ms(p.get('matched_at') or p.get('match_time') or p.get('timestamp'),
                's' if source=='CLOB_REST' or p.get('match_time') is not None else 'ms'),
            'received_ms':received_ms,'fee_paid':None,'fee_rate_bps':leg.get('fee_rate_bps',p.get('fee_rate_bps')),
            'updates_inventory':False,'requires_reconciliation':status not in {'MATCHED','MATCHED_NOT_BROADCASTED','MINED','CONFIRMED'},
            'source':source})
    return {'trade_id':p['id'],'legs':result,'unattributed':not result,'source':source,
        'requires_reconciliation':not result or any(r['requires_reconciliation'] for r in result)}


def operation_preview(operation, parameters):
    methods={'cancel_order':('cancel_order',{'order_id'}),'cancel_orders':('cancel_orders',{'order_ids'}),
        'get_order':('get_order',{'order_id'}),'list_open_orders':('list_open_orders',{'asset_id','condition_id'}),
        'list_trades':('list_trades',{'asset_id','condition_id'}),
        'balance':('GET /balance-allowance',{'asset_type','token_id'})}
    if operation not in methods:raise ValueError('Unsupported offline operation')
    name,allowed=methods[operation]
    if set(parameters)-allowed:raise ValueError('Only non-secret operation parameters accepted')
    if operation in {'cancel_order','get_order'} and not parameters.get('order_id'):raise ValueError('Order id required')
    if operation=='cancel_orders' and not parameters.get('order_ids'):raise ValueError('Order ids required')
    return {'venue':'polymarket.com','operation':operation,'sdk_or_route':name,'parameters':parameters,
        'signed':False,'sendable':False,'network_calls':0,
        'limitation':'Offline descriptor only; cancel acknowledgement cannot determine unobserved fills'}


def replace_preview(order_id,new_order_preview):
    if new_order_preview.get('sendable') is not False:raise ValueError('Unsigned preview required')
    return {'operation':'CANCEL_THEN_REQUOTE','steps':[operation_preview('cancel_order',{'order_id':order_id}),new_order_preview],
        'requires_cancel_and_fill_reconciliation_before_new_intent':True,'sendable':False,'network_calls':0}


def normalize_submit(raw,intent_id,source='SDK'):
    from .contract import normalize_sdk_response
    if source=='SDK':return normalize_sdk_response(raw,intent_id)
    if source!='CLOB_REST':raise ValueError('Explicit submit response source required')
    return normalize_sdk_response({'ok':raw.get('success'),'order_id':raw.get('orderID'),
        'status':raw.get('status'),'trade_ids':raw.get('tradeIDs',[]),'code':raw.get('code')},intent_id)
