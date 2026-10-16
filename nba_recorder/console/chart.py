"""Read-only, independently paginated historical projection. Never constructs OMS."""
import json
from bisect import bisect_right
from ..fair_value.store import read_connection
from ..inplay.policy import BookPolicy
from ..inplay.reference import book_data_risk


def query(path, asset, start, end, cursors=None, limit=600):
    if not 0<=start<=end or end-start>14460000 or not 1<=limit<=1000:
        raise ValueError('bounded chart query required')
    cursors=cursors or {}; db=read_connection(path)
    try:
        db.execute('BEGIN')
        policy=BookPolicy(**json.loads(db.execute('SELECT config FROM pe_control WHERE id=1').fetchone()[0]))
        result={'prices':[],'public_trades':[],'orders':[],'paper_fills':[],'events':[],'accounts':[],
                'cursors':dict(cursors),'has_more':False,'time_basis':'strategy_available_ms / locally known time',
                'execution_source':'PAPER_EXECUTION','data_source':'UNKNOWN','precision':'raw observed steps; paginated, no silent event thinning',
                'public_side_semantics':'UNKNOWN aggressor; raw reported side retained, no mirrored-volume aggregation',
                'real_submission_calls':0}
        origin=db.execute("SELECT payload FROM ip_session_config WHERE id='console_origin'").fetchone()
        if origin:result.update(json.loads(origin[0]))
        rows=list(db.execute('SELECT rowid,time_ms,payload FROM ip_inputs WHERE rowid>? AND time_ms<=? ORDER BY rowid LIMIT ?', (cursors.get('input',0),end,limit+1)))
        result['has_more']=len(rows)>limit
        for row in rows[:limit]:
            result['cursors']['input']=row[0]
            e=json.loads(row[2]);at=e['time_ms']
            if not origin and 'synthetic' in e.get('provenance','').lower():result['data_source']='SYNTHETIC_DATA'
            if at<start:continue
            for f in e['frames']:
                if f['asset_id']!=asset:continue
                b=f.get('book') or {};reference_errors=book_data_risk(f,policy,at)
                # Thin depth can reject quoting/valuation without erasing an
                # actually observed valid BBO from this information-only chart.
                errors=[e for e in reference_errors if e!='depth_drained']
                result['prices'].append({'id':e['id']+':'+asset,'time_ms':at,'book_ref':f.get('book_ref'),'bid':b.get('bid'),'ask':b.get('ask'),
                    'mid':str((__import__('decimal').Decimal(b['bid'])+__import__('decimal').Decimal(b['ask']))/2) if not errors else None,
                    'valid_until_ms':min(at+policy.book_max_age_ms,(f.get('book_received_ms') or 0)+policy.book_max_age_ms),
                    'quality':errors,'reference_quality':reference_errors,'exchange_ms':b.get('exchange_timestamp'),'received_ms':f.get('book_received_ms'),
                    'processed_ms':f.get('processed_ms'),'strategy_available_ms':at,'input_phase_unverified':f.get('phase'),'source':e.get('provenance'),
                    'inventory':None,'pnl':None})
            for trade_index,t in enumerate(e.get('trades',[])):
                if t.get('asset_id')==asset:
                    result['public_trades'].append({**t,'id':t.get('event_id') or e['id']+':trade:'+str(trade_index),'time_ms':at,'aggressor_side':'UNKNOWN','execution':'PUBLIC_MARKET_PRINT'})
        rows=list(db.execute('SELECT seq,time_ms,kind,payload FROM pe_journal WHERE seq>? AND time_ms<=? ORDER BY seq LIMIT ?', (cursors.get('journal',0),end,limit+1)))
        result['has_more'] |= len(rows)>limit
        for row in rows[:limit]:
            seq,at,kind,raw=tuple(row);result['cursors']['journal']=seq
            p=json.loads(raw);o=p.get('order');detail=p.get('detail') or {}
            if not isinstance(detail,dict):detail={'raw_detail':detail}
            if at<start:continue
            if o and o.get('asset_id')==asset:
                authority=detail.get('authority') if isinstance(detail,dict) else None
                accepted=bool(authority and authority.get('state') in {'OPEN','PARTIALLY_FILLED','FILLED','CANCELLED'})
                result['orders'].append({'id':str(seq),'time_ms':at,'order_id':o['id'],'state':o['state'],
                    'accepted_evidence':accepted,'authority_state':(authority or {}).get('state'), 'side':o['side'],'price':o['price'],
                    'size':o['size'],'filled':o['filled'],'kind':kind,'remaining':str(__import__('decimal').Decimal(o['size'])-__import__('decimal').Decimal(o['filled']))})
                if accepted and __import__('decimal').Decimal(str(detail.get('delta_fill','0')))>0:
                    result['paper_fills'].append({'id':detail.get('report_id') or str(seq),'time_ms':at,'exchange_ms':detail.get('exchange_ms'),
                        'received_ms':at,'processed_ms':at,'timing_note':'Paper OMS receipt availability; separate upstream transport receipt not recorded here',
                        'price':o['price'],'quantity':detail['delta_fill'],'side':o['side'],'execution':'PAPER_EXECUTION','order_id':o['id']})
            if kind in {'KILL_SWITCH','RISK','PAUSED','OPERATOR_RESUME','CONSOLE_COMMAND_RECEIPT','SERVICE_RESTART_UNKNOWN'}:
                compact = {'reason':detail.get('reason'),'order_id':(o or {}).get('id'),'raw_journal_seq':seq} if kind=='RISK' else detail
                result['events'].append({'id':'journal:'+str(seq),'time_ms':at,'kind':kind,'detail':compact,'raw_journal_seq':seq,'risk_state':p.get('control',{}).get('state')})
            if kind in {'BOOK_PAPER_EQUITY','EXECUTION_REPORT','RECONCILED','COLLATERAL_FUNDED_PAPER_SPLIT','VIRTUAL_ASSET_SEED'}:
                account=json.loads(p['control']['account']) if isinstance(p['control'].get('account'),str) else p['control'].get('account')
                result['accounts'].append({'id':str(seq),'time_ms':at,'inventory':(account or {}).get('positions',{}).get(asset),
                    'paper_pnl':detail.get('paper_pnl'),'realized':detail.get('ledger_valuation',{}).get('realized_trading_pnl'),
                    'unrealized':detail.get('ledger_valuation',{}).get('unrealized_trading_pnl'),'source':'as-of OMS/FIFO journal: '+kind,'marks':detail.get('marks')})
        # Phase transitions are separate evidence, never copied from current state.
        rows=list(db.execute('SELECT id,time_ms,payload FROM ip_phase_log WHERE id>? AND time_ms<=? ORDER BY id LIMIT ?', (cursors.get('phase',0),end,limit+1)))
        result['has_more'] |= len(rows)>limit
        previous=None
        for r in rows[:limit]:
            result['cursors']['phase']=r[0];p=json.loads(r[2]);key=(p['state'],p.get('period'))
            if r[1]>=start and key!=previous:
                result['events'].append({'id':'phase:'+str(r[0]),'time_ms':r[1],'kind':'PHASE','detail':p})
            previous=key
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='ip_game_observations'").fetchone():
            rows=list(db.execute('SELECT id,time_ms,payload FROM ip_game_observations WHERE id>? AND time_ms<=? ORDER BY id LIMIT ?', (cursors.get('game',0),end,limit+1)))
            result['has_more'] |= len(rows)>limit
            for r in rows[:limit]:
                result['cursors']['game']=r[0]
                if r[1]>=start:result['events'].append({'id':'game:'+str(r[0]),'time_ms':r[1],'kind':'GAME_INFORMATION','detail':json.loads(r[2])})
        return result
    finally:db.close()
