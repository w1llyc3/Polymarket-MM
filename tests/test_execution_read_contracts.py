import copy
import json
import tempfile
import unittest
from pathlib import Path
import httpx
from nba_recorder.execution.account import public_positions
from nba_recorder.execution.normalization import normalize_order,normalize_trade,normalize_cancel,normalize_submit,operation_preview,replace_preview,timestamp_ms
from nba_recorder.execution.read_account import MockAccountReadPort,ReadAccountJournal,account_snapshot,compare_account,UnconfiguredAccountReadPort
from nba_recorder.inplay.observations import price_point
from nba_recorder.inplay.policy import BookPolicy
from nba_recorder.inplay.demo import frame,AT

ROOT=Path(__file__).resolve().parents[1]


class MappingTests(unittest.TestCase):
    def order(self):return {'id':'own','condition_id':'c','asset_id':'1','side':'BUY','price':'.45','original_size':'10','size_matched':'2','status':'LIVE','expires_at':'2026-10-21T00:00:00Z'}
    def test_rest_sdk_order_quantities_and_terminal_remainder(self):
        sdk=normalize_order(self.order())
        self.assertEqual(sdk['filled'],'2');self.assertEqual(sdk['remaining'],'8');self.assertEqual(sdk['state'],'PARTIALLY_FILLED')
        rest={**self.order(),'market':'c','expiration':'1792540800','created_at':1792540800}
        parsed=normalize_order(rest,'CLOB_REST');self.assertEqual(parsed['condition_id'],'c');self.assertEqual(parsed['filled'],'2')
        self.assertEqual(parsed['created_ms'],1792540800000)
        self.assertEqual(normalize_order({**self.order(),'created_at':'2026-10-21T00:00:00Z'})['created_ms'],1792540800000)
        canceled=normalize_order({**self.order(),'status':'CANCELED_MARKET_RESOLVED'})
        self.assertEqual(canceled['state'],'CANCELLED');self.assertEqual(canceled['filled'],'2')
        self.assertEqual(normalize_order({**self.order(),'status':'MATCHED'})['state'],'UNKNOWN')
        self.assertEqual(normalize_order({**self.order(),'size_matched':'10','status':'MATCHED'})['state'],'FILLED')
    def test_raw_and_sdk_stream_time_units(self):
        raw={**self.order(),'market':'c','timestamp':'1792540800123','type':'UPDATE'}
        self.assertEqual(normalize_order(raw,'USER_RAW')['exchange_ms'],1792540800123)
        sdk={**self.order(),'timestamp':'2026-10-21T00:00:00.123Z','order_event_type':'UPDATE'}
        self.assertEqual(normalize_order({'topic':'user','type':'order','payload':sdk},'USER_SDK')['exchange_ms'],1792540800123)
        with self.assertRaises(ValueError):timestamp_ms('2026-10-21T00:00:00')
    def test_partial_cancel_response_does_not_assume_all_canceled(self):
        result=normalize_cancel({'canceled':['one'],'not_canceled':{'two':'already matched'}},['one','two','three'])
        self.assertEqual([r['state'] for r in result],['CANCELLED','UNKNOWN','UNKNOWN'])
        self.assertTrue(all(r['requires_fill_query'] and r['filled_quantity'] is None for r in result))
    def test_rest_submit_and_replace_are_descriptions_not_sends(self):
        result=normalize_submit({'success':True,'orderID':'one','status':'matched','tradeIDs':['trade']},'intent','CLOB_REST')
        self.assertEqual(result['state'],'UNKNOWN');self.assertIsNone(result['filled_quantity'])
        preview=replace_preview('one',{'sendable':False,'sdk_intent':{}})
        self.assertFalse(preview['sendable']);self.assertTrue(preview['requires_cancel_and_fill_reconciliation_before_new_intent'])
        with self.assertRaises(ValueError):operation_preview('sign',{})
        self.assertFalse(operation_preview('get_order',{'order_id':'one'})['sendable'])
    def test_maker_uses_own_leg_not_total_trade_size_or_counterparty_side(self):
        p={'id':'t','market':'c','asset_id':'other-asset','side':'SELL','size':'100','price':'.45','status':'TRADE_STATUS_CONFIRMED',
           'trader_side':'MAKER','match_time':'1792540800','maker_orders':[
               {'order_id':'own','asset_id':'1','side':'BUY','price':'.45','matched_amount':'2'},
               {'order_id':'other','asset_id':'1','side':'BUY','price':'.45','matched_amount':'98'}]}
        result=normalize_trade(p,{'own'},source='CLOB_REST')
        self.assertEqual(len(result['legs']),1);leg=result['legs'][0]
        self.assertEqual((leg['quantity'],leg['side'],leg['asset_id']),('2','BUY','1'))
        self.assertEqual(leg['exchange_ms'],1792540800000);self.assertIsNone(leg['fee_paid']);self.assertFalse(leg['updates_inventory'])
        self.assertTrue(normalize_trade(p,{'unrelated'},source='CLOB_REST')['unattributed'])
    def test_bad_latest_book_invalidates_mark_and_expiry_is_bounded(self):
        f=frame();good=price_point(f,BookPolicy());self.assertIsNotNone(good['mid'])
        bad=copy.deepcopy(f);bad['book']['bid']='.9'
        self.assertIsNone(price_point(bad,BookPolicy())['mid'])
        self.assertLessEqual(good['valid_until_ms'],f['book_received_ms']+3000)


class PublicV2Tests(unittest.TestCase):
    def test_cursor_envelope_and_current_size_are_not_lifetime_size(self):
        requests=[]
        def handle(request):
            requests.append(request)
            cursor=request.url.params.get('cursor')
            return httpx.Response(200,json={'data':[{'token_id':'1' if not cursor else '2','condition_id':'c','current_size':2,'total_size':100,'avg_price':.5}],
                'pagination':{'has_more':not bool(cursor),'next_cursor':'opaque' if not cursor else None}})
        with httpx.Client(transport=httpx.MockTransport(handle)) as client:result=public_positions('0x'+'1'*40,client)
        self.assertEqual(len(requests),2);self.assertTrue(result['positions_complete'])
        self.assertEqual([p['quantity'] for p in result['positions']],['2','2'])
        self.assertTrue(all(r.url.path=='/v2/positions' and r.method=='GET' and r.url.params['user']=='0x'+'1'*40 for r in requests))
        self.assertEqual(requests[1].url.params['status'],'OPEN');self.assertIsNone(result['cash_balance'])
        self.assertFalse(result['authenticated_account_connected'])
    def test_cursor_conflict_is_not_complete(self):
        def handle(request):return httpx.Response(200,json={'data':[],'pagination':{'has_more':True,'next_cursor':None}})
        with httpx.Client(transport=httpx.MockTransport(handle)) as client:
            with self.assertRaises(ValueError):public_positions('0x'+'1'*40,client)
    def test_empty_v2_is_valid_not_fake_cash(self):
        with httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'data':[],'pagination':{'has_more':False,'next_cursor':None}}))) as client:
            result=public_positions('0x'+'1'*40,client)
        self.assertEqual(result['positions'],[]);self.assertIsNone(result['equity'])


class ReadJournalTests(unittest.TestCase):
    def fixture(self):return json.loads((ROOT/'examples/account-read-fixture.json').read_text(encoding='utf-8'))
    def test_balance_orders_trades_paging_and_restart_dedup(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'read.sqlite'
            first=account_snapshot(MockAccountReadPort(self.fixture()),path)
            second=account_snapshot(MockAccountReadPort(self.fixture()),path)
            self.assertEqual(first['cash_balance'],'100');self.assertEqual(first['available_cash_after_local_order_reservations'],'96.40')
            self.assertEqual(len(second['confirmed_fills']),1);self.assertEqual(second['confirmed_fills'][0]['quantity'],'2')
            self.assertEqual(second['provisional_fills'],[]);self.assertFalse(second['authenticated_account_connected'])
            self.assertTrue(all(c.startswith('GET_') for c in first['read_calls']))
            local={'cash':'100','positions':first['positions'],'orders':first['orders']}
            self.assertEqual(compare_account(local,first)['status'],'MATCH')
            self.assertEqual(compare_account({**local,'cash':'99'},first)['status'],'MISMATCH')
            self.assertFalse(compare_account(local,first)['resume_allowed'])
    def test_stream_partial_fill_and_failed_settlement_remain_read_evidence(self):
        fixture=self.fixture();trade=fixture['trades_pages'][0]['items'][0]
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'read.sqlite';journal=ReadAccountJournal(path)
            try:
                journal.apply_trade(normalize_trade(trade,{'own-buy'}))
                journal.apply_trade(normalize_trade({**trade,'status':'CONFIRMED'},{'own-buy'}))
                journal.apply_trade(normalize_trade({**trade,'status':'CONFIRMED'},{'own-buy'}))
                self.assertEqual(len(journal.snapshot()['confirmed_fills']),1)
                journal.apply_trade(normalize_trade({**trade,'status':'FAILED'},{'own-buy'}))
                snapshot=journal.snapshot();self.assertEqual(len(snapshot['failed_fills']),1)
                self.assertFalse(snapshot['mutates_inventory']);self.assertEqual(snapshot['confirmed_fills'],[])
            finally:journal.close()
    def test_unconfigured_port_never_creates_authentication(self):
        with self.assertRaises(RuntimeError):UnconfiguredAccountReadPort().balance()
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):account_snapshot(UnconfiguredAccountReadPort(),Path(d)/'read.sqlite')
