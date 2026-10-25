"""Separate posthoc terminal inventory valuation, never a strategy input.

Recorded public resolution is an accounting assumption, not actual redemption.
Original/replay ledgers and decision streams remain unchanged.
"""
import csv
import hashlib
import json
from decimal import Decimal
from pathlib import Path

from ..fair_value.store import read_connection


def terminal_value(account, market, raw, received_ms):
    assets={o['asset_id'] for o in market['outcomes']}
    winner=raw.get('winning_asset_id')
    winner_label=next((o.get('label') for o in market['outcomes'] if o['asset_id']==winner),None)
    if (len(assets)!=2 or raw.get('event_type')!='market_resolved'
            or str(raw.get('id'))!=str(market['market_id'])
            or raw.get('market')!=market['condition_id']
            or set(raw.get('assets_ids',[]))!=assets or winner not in assets
            or (winner_label is not None and raw.get('winning_outcome')!=winner_label)
            or type(received_ms) is not int
            or not 0<=int(raw.get('timestamp',-1))<=received_ms):
        raise ValueError('Resolution identity/timestamp not verified')
    positions={a:Decimal(q) for a,q in account['positions'].items()}
    if set(positions)-assets or any(not q.is_finite() or q<0 for q in positions.values()):
        raise ValueError('Unsupported inventory')
    cash=Decimal(account['cash']);capital=Decimal(account['capital_contributions'])
    if not cash.is_finite() or not capital.is_finite():raise ValueError('Invalid account')
    payout=positions.get(winner,Decimal(0))
    return {'basis':'POSTHOC_RECORDED_WINNER_1_0_VALUATION_NOT_ACTUAL_REDEMPTION',
            'winning_asset_id':winner,'winning_outcome':raw.get('winning_outcome'),
            'resolution_receive_ms':received_ms,'assumed_inventory_payout':str(payout),
            'assumed_terminal_equity':str(cash+payout),
            'simulated_terminal_pnl':str(cash+payout-capital),
            'recorded_simulated_fees':account['fees'],'recorded_simulated_rewards':account['rewards'],
            'live_go':False,'real_submission_calls':0}


def publish(root,day,output):
    root,output=Path(root),Path(output)
    state=json.loads((root/'data/unattended'/day/'state.json').read_text(encoding='utf-8'))
    games=json.loads((output/'summary.json').read_text(encoding='utf-8'));rows=[];evidence=[]
    for game in games:
        key=game['market_id'];entry=state['games'][key];market=entry['market']
        source=root/'data/console'/(entry['session_id']+'-source.sqlite')
        db=read_connection(source)
        try:
            found=list(db.execute("SELECT event_key,local_receive_timestamp,payload FROM raw_events WHERE event_type='market_resolved' ORDER BY local_receive_timestamp"))
        finally:db.close()
        resolutions=[]
        for r in found:
            raw=json.loads(r['payload'])
            if str(raw.get('id'))==key:
                # Validate every matching message, including conflicts.
                terminal_value(game['lanes'][0]['account'],market,raw,r['local_receive_timestamp'])
                resolutions.append((r,raw))
        if not resolutions:raise ValueError('No captured resolution: '+key)
        if len({raw['winning_asset_id'] for _,raw in resolutions})!=1:raise ValueError('Conflicting resolutions: '+key)
        r,raw=resolutions[0]
        evidence.append({'market_id':key,'source_file':source.relative_to(root).as_posix(),
                         'event_key':r['event_key'],'receive_ms':r['local_receive_timestamp'],
                         'raw_payload_sha256':hashlib.sha256(r['payload'].encode()).hexdigest(),'raw':raw})
        for lane in game['lanes']:
            if lane['mode']!='REDECODED':continue
            account=lane['account']
            value=terminal_value(account,market,raw,r['local_receive_timestamp'])
            # Independently reproduce the same endpoint from FIFO lots; never
            # append SETTLEMENT or create a fill in either historical ledger.
            from ..fill_management.ledger import Ledger,valuation
            lane_db=read_connection(output/key/Path(lane['result']).parent/'paper.sqlite')
            try:
                run_id=lane_db.execute("SELECT run_id FROM l6_accounts WHERE lane='UNIFIED_PAPER'").fetchone()[0]
                ledger=Ledger(lane_db,run_id,'UNIFIED_PAPER');ledger.audit()
                marks={o['asset_id']:('1' if o['asset_id']==raw['winning_asset_id'] else '0') for o in market['outcomes']}
                fifo=valuation(ledger.state(),marks)
                if Decimal(fifo['net_pnl'])!=Decimal(value['simulated_terminal_pnl']):
                    raise ValueError('Terminal valuation does not reconcile to FIFO ledger')
                value['fifo_terminal_valuation_reconciled']=True
            finally:lane_db.close()
            rows.append({'market_id':key,'question':game['question'],'model':lane['model'],
                         'source_tape_id':game['source_tape_id'],'derived_input_sha256':lane['derived_input_sha256'],
                         'fill_events':lane['fills'],'risk_state_at_input_end':lane['risk_state'],
                         'realized_before_terminal_valuation':account['realized_trading_pnl'],
                         'pre_terminal_model_net_pnl':account['net_pnl'],
                         'virtual_cash':account['cash'],'positions':account['positions'],**value})
    payload={'date':day,'mode':'INDEPENDENT_POSTHOC_PAPER',
             'assumptions':['Original strategy and risk unchanged; endpoint valuation never feeds decisions',
                            'Remaining inventory valued at recorded winner $1, loser $0; no actual redemption or invented liquidation',
                            'Zero simulated fees, zero simulated rebates/rewards; not actual profit'],
             'resolution_evidence':evidence,'rows':rows}
    (output/'terminal-valuation.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
    with (output/'terminal-valuation.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader()
        writer.writerows([{**r,'positions':json.dumps(r['positions'],sort_keys=True)} for r in rows])
    return payload
