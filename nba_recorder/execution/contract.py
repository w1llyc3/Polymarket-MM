from typing import Protocol, runtime_checkable


@runtime_checkable
class PaperAuthority(Protocol):
    mode: str
    real_submission_calls: int

    def submit(self, order, at, response_ms=5, accepted=True, drop_response=False): ...
    def cancel(self, order_id, at, latency_ms, drop_response=False): ...
    def apply_cancel(self, order_id, at, payload): ...
    def authoritative(self, order_id): ...
    def order_ids(self): ...
    def fill(self, order_id, size, at, event_id, transport_ms=0, observed_ms=None): ...
    def balance_snapshot(self, seed_cash, seed_positions): ...


class DisabledLiveAdapter:
    """No configuration, signing credentials, SDK trading client, or sender."""
    mode = 'DISABLED'
    real_submission_calls = 0

    def submit(self, *args, **kwargs):
        raise PermissionError('Real trading is not implemented or authorized')

    cancel = submit
    sign = submit
    transfer = submit


def preview_order(market, target):
    """Unsigned SDK-intent mapping, NOT an exchange POST body."""
    from ..fair_value.maths import decimal
    if market.get('venue') != 'polymarket.com' or market.get('version') not in {'v1','v2'}:
        raise ValueError('Explicit Polymarket.com supported market version required')
    asset=target['asset_id']
    if asset not in {o['asset_id'] for o in market['outcomes']}:
        raise ValueError('Asset not bound to outcome')
    price,size,tick=map(decimal,(target['price'],target['size'],market['tick_size']))
    if target['side'] not in {'BUY','SELL'} or not 0<price<1 or tick<=0 or price%tick or size<decimal(market['minimum_order_size']):
        raise ValueError('Invalid order preview')
    return {'venue':'polymarket.com','market_id':market['market_id'],'market_version':market['version'],
        'identifier_kind':'position_id' if market['version']=='v2' else 'token_id',
        'sdk_intent':{'asset_id':asset,'side':target['side'],'price':str(price),'size':str(size),'post_only':True},
        'time_in_force':'GTC','signed':False,'sendable':False,'network_calls':0,
        'fees':'UNKNOWN: must read actual market fee rules before live use',
        'limitation':'SDK intent only; no maker/taker amounts, signature, auth, approval or executable HTTP payload'}


def normalize_sdk_response(response, intent_id):
    """Offline mapping of documented unified Python OrderResponse fields.

    Matching/delay acknowledgements have no cumulative fill quantity: query
    authoritative order/fills before updating inventory. No sender is invoked.
    """
    from ..fair_value.providers import reject_secrets
    reject_secrets(response)
    if not isinstance(response,dict):raise ValueError('Explicit SDK response dictionary required')
    if response.get('ok') is False:
        return {'intent_id':intent_id,'order_id':response.get('order_id'),'state':'REJECTED',
                'reason_code':response.get('code'),'filled_quantity':None,'requires_reconciliation':False}
    identity=response.get('order_id');status=response.get('status')
    proven=response.get('ok') is True and isinstance(identity,str) and bool(identity)
    state='OPEN' if proven and status=='live' else 'UNKNOWN'
    return {'intent_id':intent_id,'order_id':identity if proven else None,'state':state,
        'exchange_status':status,'filled_quantity':None,'requires_reconciliation':state=='UNKNOWN',
        'trade_ids':response.get('trade_ids',[]),'assumption':'acknowledgement alone never creates a fill receipt'}
