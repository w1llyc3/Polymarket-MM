"""Public Polymarket.com positions only. No private key or authenticated requests."""
import re
import httpx
from ..api import utc_ms
from ..fair_value.maths import decimal


def public_positions(address, client=None, version='v2', status='OPEN'):
    if not isinstance(address,str) or not re.fullmatch('0x[0-9a-fA-F]{40}',address):
        raise ValueError('Public wallet address required; never provide a private key')
    owned=client is None
    client=client or httpx.Client(timeout=10,follow_redirects=False)
    rows=[]
    complete=False
    try:
        if version=='v2':
            if status not in {'OPEN','CLOSED','REDEEMABLE','REDEEMABLE_LOST','MERGEABLE'}:raise ValueError('Position lifecycle filter required')
            cursor=None;seen=set();pages=0
            while pages<20:
                params={'user':address,'limit':500,'status':status,'filter_type':'TOKENS','filter_amount':0}
                if cursor is not None:params['cursor']=cursor
                response=client.get('https://data-api.polymarket.com/v2/positions',params=params)
                response.raise_for_status();body=response.json();pages+=1
                if not isinstance(body,dict) or not isinstance(body.get('data'),list) or 'pagination' not in body:raise ValueError('Unsupported Data API v2 envelope')
                for p in body['data']:
                    size=decimal(p['current_size'])
                    if size<0:raise ValueError('Invalid position size')
                    rows.append({'asset_id':str(p['token_id']),'condition_id':p['condition_id'],'quantity':str(size),
                        'title':p.get('title'),'outcome':p.get('outcome'),'average_price':str(p['avg_price']),
                        'api_current_value':p.get('current_value'),'redeemable':p.get('redeemable'),'status':p.get('status'),
                        'identifier_kind':'DATA_API_OUTCOME_ID_REQUIRES_MARKET_BINDING',
                        'source':'public Data API v2 /positions; indexed, not authenticated authority'})
                pagination=body['pagination'];cursor=pagination.get('next_cursor')
                if cursor is None:
                    if pagination.get('has_more'):raise ValueError('Missing next cursor')
                    complete=True;break
                if not isinstance(cursor,str) or cursor in seen:raise ValueError('Invalid or repeated cursor')
                seen.add(cursor)
            return {'mode':'PUBLIC_READ_ONLY','venue':'polymarket.com','api_version':'v2','address':address,
                'received_ms':utc_ms(),'positions':rows,'positions_complete':complete,'pages':pages,'requested_status':status,
                'cash_balance':None,'open_orders':None,'own_fills':None,'equity':None,
                'authenticated_account_connected':False,'real_submission_calls':0,
                'limitations':['Data API v2 version is not the market protocol version',
                    'Indexed positions can lag; match outcome ids against explicit v1/v2 market metadata before use',
                    'Public positions do not provide authenticated cash, own orders or a complete settlement ledger']}
        if version!='v1':raise ValueError('Unsupported Data API version')
        for offset in range(0,10000,500):
            response=client.get('https://data-api.polymarket.com/positions',params={'user':address,'limit':500,'offset':offset,'sizeThreshold':0})
            response.raise_for_status(); page=response.json()
            if not isinstance(page,list):raise ValueError('Unsupported public positions schema')
            for p in page:
                size=decimal(p['size'])
                if size<0:raise ValueError('Invalid position size')
                rows.append({'asset_id':p['asset'],'condition_id':p.get('conditionId'),'quantity':str(size),
                    'title':p.get('title'),'outcome':p.get('outcome'), 'average_price':str(p.get('avgPrice')),
                    'api_current_value':p.get('currentValue'),'redeemable':p.get('redeemable'),
                    'source':'public Data API /positions v1, token identifiers; not authenticated account authority'})
            if len(page)<500:
                complete=True;break
        return {'mode':'PUBLIC_READ_ONLY','venue':'polymarket.com','address':address,'received_ms':utc_ms(),
            'api_version':'v1',
            'positions':rows,'positions_complete':complete, 'cash_balance':None,'open_orders':None,'own_fills':None,
            'authenticated_account_connected':False,'equity':None,'real_submission_calls':0,
            'limitations':['Public indexed positions may lag; not authoritative balance/order reconciliation',
                           'No cash, allowances, authenticated orders or user stream requested',
                           'Protocol V2 account holdings require v2 Data API mapping; not inferred from v1 positions']}
    finally:
        if owned:client.close()
