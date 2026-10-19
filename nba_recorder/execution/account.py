"""Public Polymarket.com positions only. No private key or authenticated requests."""
import re
import httpx
from ..api import utc_ms
from ..fair_value.maths import decimal


def public_positions(address, client=None):
    if not isinstance(address,str) or not re.fullmatch('0x[0-9a-fA-F]{40}',address):
        raise ValueError('Public wallet address required; never provide a private key')
    owned=client is None
    client=client or httpx.Client(timeout=10,follow_redirects=False)
    rows=[]
    complete=False
    try:
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
            'positions':rows,'positions_complete':complete, 'cash_balance':None,'open_orders':None,'own_fills':None,
            'authenticated_account_connected':False,'equity':None,'real_submission_calls':0,
            'limitations':['Public indexed positions may lag; not authoritative balance/order reconciliation',
                           'No cash, allowances, authenticated orders or user stream requested',
                           'Protocol V2 account holdings require v2 Data API mapping; not inferred from v1 positions']}
    finally:
        if owned:client.close()
