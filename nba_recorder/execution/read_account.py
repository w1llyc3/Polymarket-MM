"""Credential-free authenticated-read contract exercised by an explicit Mock port."""
import json
import sqlite3
from pathlib import Path
from ..api import utc_ms
from ..fair_value.maths import decimal
from ..fair_value.store import digest
from .normalization import normalize_order,normalize_trade


class UnconfiguredAccountReadPort:
    mode='UNCONFIGURED'
    def balance(self):raise RuntimeError('Authenticated read transport not configured; no signer is created')
    def orders(self,cursor=None):return self.balance()
    def trades(self,cursor=None):return self.balance()
    def positions(self):return self.balance()
    def order(self,identity):return self.balance()


class MockAccountReadPort:
    mode='MOCK_AUTHENTICATED_READ_ONLY'
    def __init__(self,fixture):
        from ..fair_value.providers import reject_secrets
        reject_secrets(fixture)
        self.fixture=fixture;self.calls=[]
    def balance(self):self.calls.append('GET_BALANCE');return self.fixture['balance']
    def positions(self):self.calls.append('GET_POSITIONS');return self.fixture['positions']
    def _page(self,key,cursor):
        self.calls.append('GET_'+key.upper());pages=self.fixture[key]
        if cursor is None:return pages[0]
        for i,p in enumerate(pages[:-1]):
            if p['next_cursor']==cursor:return pages[i+1]
        raise ValueError('Unknown fixture cursor')
    def orders(self,cursor=None):return self._page('orders_pages',cursor)
    def trades(self,cursor=None):return self._page('trades_pages',cursor)
    def order(self,identity):
        self.calls.append('GET_ORDER')
        return next((o for p in self.fixture['orders_pages'] for o in p['items'] if o['id']==identity),None)


class ReadAccountJournal:
    """Normalized read evidence only. Never updates OMS inventory or sends orders."""
    def __init__(self,path):
        self.db=sqlite3.connect(path)
        self.db.executescript('''CREATE TABLE IF NOT EXISTS read_orders(id TEXT PRIMARY KEY,payload TEXT);
        CREATE TABLE IF NOT EXISTS read_legs(id TEXT PRIMARY KEY,payload TEXT);
        CREATE TABLE IF NOT EXISTS read_evidence(seq INTEGER PRIMARY KEY,id TEXT UNIQUE,payload TEXT);''')
    def close(self):self.db.close()
    def apply_order(self,order):
        with self.db:
            old=self.db.execute('SELECT payload FROM read_orders WHERE id=?',(order['id'],)).fetchone()
            if old:
                previous=json.loads(old[0])
                if previous.get('exchange_ms') is not None and order.get('exchange_ms') is not None and order['exchange_ms']<previous['exchange_ms']:return False
                if decimal(order['filled'])<decimal(previous['filled']):raise ValueError('Cumulative filled quantity regressed')
            signature=digest(order)
            if not self.db.execute('INSERT OR IGNORE INTO read_evidence(id,payload) VALUES(?,?)',(signature,json.dumps(order))).rowcount:return False
            self.db.execute('INSERT OR REPLACE INTO read_orders VALUES(?,?)',(order['id'],json.dumps(order)))
            return True
    def apply_trade(self,trade):
        with self.db:
            for leg in trade['legs']:
                old=self.db.execute('SELECT payload FROM read_legs WHERE id=?',(leg['fill_id'],)).fetchone()
                if old:
                    previous=json.loads(old[0])
                    if any(previous[k]!=leg[k] for k in ('order_id','asset_id','side','quantity','price')):raise ValueError('Trade-leg identity changed')
                    if previous['confirmed'] and not leg['failed']:continue
                    if previous['failed'] and not leg['failed']:continue
                signature=digest(leg)
                if not self.db.execute('INSERT OR IGNORE INTO read_evidence(id,payload) VALUES(?,?)',(signature,json.dumps(leg))).rowcount:continue
                self.db.execute('INSERT OR REPLACE INTO read_legs VALUES(?,?)',(leg['fill_id'],json.dumps(leg)))
    def snapshot(self):
        legs=[json.loads(r[0]) for r in self.db.execute('SELECT payload FROM read_legs ORDER BY id')]
        return {'orders':[json.loads(r[0]) for r in self.db.execute('SELECT payload FROM read_orders ORDER BY id')],
            'confirmed_fills':[l for l in legs if l['confirmed'] and not l['failed']],
            'provisional_fills':[l for l in legs if not l['confirmed'] and not l['failed']],
            'failed_fills':[l for l in legs if l['failed']], 'mutates_inventory':False}


def paged(method):
    result=[];cursor=None;seen=set()
    for _ in range(100):
        page=method(cursor)
        result.extend(page['items']);next_cursor=page.get('next_cursor')
        if not page.get('has_more'):
            if next_cursor is not None:raise ValueError('Contradictory SDK page')
            return result,True
        if not next_cursor or next_cursor in seen:raise ValueError('Missing/repeated SDK cursor')
        seen.add(next_cursor);cursor=next_cursor
    return result,False


def account_snapshot(port, journal_path):
    if port.mode!='MOCK_AUTHENTICATED_READ_ONLY':raise ValueError('Only the explicit Mock read port is wired; real auth remains unconfigured')
    received=utc_ms();balance=port.balance();orders,orders_complete=paged(port.orders);trades,trades_complete=paged(port.trades)
    positions=port.positions();journal=ReadAccountJournal(journal_path)
    try:
        normalized=[normalize_order(o,received_ms=received) for o in orders]
        for order in normalized:journal.apply_order(order)
        owned={o['id'] for o in normalized}|set(port.fixture.get('known_order_ids',[]))
        unattributed=0
        for trade in trades:
            result=normalize_trade(trade,owned,received_ms=received);unattributed+=result['unattributed'];journal.apply_trade(result)
        for event in port.fixture.get('user_events',[]):
            if event['type']=='order':journal.apply_order(normalize_order(event,'USER_SDK',received))
            elif event['type']=='trade':journal.apply_trade(normalize_trade(event,owned,received,'USER_SDK'))
        data=journal.snapshot()
        cash=decimal(balance['balance'])/decimal(1000000)
        holdings={str(p['token_id']):str(decimal(p['current_size'])) for p in positions}
        reserved=sum((decimal(o['remaining'])*decimal(o['price']) for o in data['orders'] if o['state'] in {'OPEN','PARTIALLY_FILLED','UNKNOWN'} and o['side']=='BUY'),decimal(0))
        return {'mode':port.mode,'venue':'polymarket.com','received_ms':received,'cash_balance':str(cash),
            'read_status':'NEEDS_REVIEW' if data['failed_fills'] or unattributed or not orders_complete or not trades_complete else 'MOCK_READ_COMPLETE',
            'equity':None,
            'balance_unit':'explicit raw fixed-6 collateral units converted to decimal',
            'allowances':{k:str(decimal(v)/decimal(1000000)) for k,v in balance.get('allowances',{}).items()},
            'available_cash_after_local_order_reservations':str(cash-reserved),'positions':holdings,
            'orders_complete':orders_complete,'trades_complete':trades_complete,'unattributed_trades':unattributed,
            **data,'read_calls':list(port.calls),'authenticated_account_connected':False,
            'real_submission_calls':0,'live_go':False,
            'limitations':['Mock auth-read contract only; no live credentials, SDK account or user subscription connected',
                'Matched/mined/confirmed updates share a fill identity; failed settlement requires review, never automatic inventory repair',
                'Read evidence does not establish initial cost basis or actual account equity']}
    finally:journal.close()


def compare_account(local,remote):
    """Independent read snapshot comparison; no automatic reset/resume."""
    if not remote.get('orders_complete') or not remote.get('trades_complete') or remote.get('unattributed_trades') or remote.get('failed_fills') or any(o['state']=='UNKNOWN' for o in remote.get('orders',[])):
        return {'status':'INCOMPLETE','resume_allowed':False}
    differences=[]
    if decimal(local['cash'])!=decimal(remote['cash_balance']):differences.append('cash')
    keys=set(local['positions'])|set(remote['positions'])
    differences.extend('position:'+a for a in keys if decimal(local['positions'].get(a,'0'))!=decimal(remote['positions'].get(a,'0')))
    local_orders={o['id']:o for o in local.get('orders',[])};remote_orders={o['id']:o for o in remote['orders']}
    if set(local_orders)!=set(remote_orders):differences.append('order_ids')
    for identity in set(local_orders)&set(remote_orders):
        if any(str(local_orders[identity].get(k))!=str(remote_orders[identity].get(k)) for k in ('filled','state')):differences.append('order:'+identity)
    return {'status':'MATCH' if not differences else 'MISMATCH','differences':sorted(differences),'resume_allowed':False,
        'mode':remote['mode'],'comparison_only':True}
