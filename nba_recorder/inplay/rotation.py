"""Opt-in, causal inventory-rotation hypothesis; only used by Paper research.

Book mid is a reference price, never a claim of fair outcome probability.
All profile state is reconstructed/persisted from already available books.
"""
import json
from ..fair_value.maths import decimal as D
from ..quote_engine.model import grid
from ..storage import encode


def decorate(db, frame, policy, at):
    from .reference import book_data_risk
    # Ignore any supplied strategy context; generate from this lane's own state.
    frame = {k: v for k, v in frame.items() if k != 'rotation'}
    if book_data_risk(frame, policy, at):
        return frame
    asset, ref = frame['asset_id'], str(frame.get('book_ref'))
    last = db.execute('SELECT payload FROM ip_rotation WHERE asset_id=? AND time_ms<=? ORDER BY time_ms DESC,rowid DESC LIMIT 1', (asset, at)).fetchone()
    previous = json.loads(last[0]) if last else {}
    mid = (D(frame['book']['bid'])+D(frame['book']['ask']))/2
    history = [json.loads(r[0]) for r in db.execute('SELECT payload FROM ip_rotation WHERE asset_id=? AND time_ms BETWEEN ? AND ? ORDER BY time_ms,rowid', (asset, at-policy.rotation_lookback_ms, at))]
    mids = [D(r['mid']) for r in history] + [mid]
    movement = max(mids)-min(mids)
    pause_until = previous.get('pause_until_ms', 0)
    if ref != str(previous.get('book_ref')) and movement >= D(policy.rotation_jump):
        pause_until = at+policy.rotation_cooldown_ms
    context = {'asof_ms': at, 'book_ref': frame.get('book_ref'), 'mid': str(mid),
               'range': str(movement), 'pause_until_ms': pause_until,
               'distance': str(min(D(policy.rotation_max_distance), max(D(policy.quote_distance), movement))),
               'profile': 'INPLAY_ROTATION_V1', 'reference': 'BOOK_MID_NOT_FAIR_VALUE'}
    # Reused watchdog/dispatch frames do not manufacture fresh observations.
    if ref != str(previous.get('book_ref')):
        db.execute('INSERT OR IGNORE INTO ip_rotation VALUES(?,?,?,?)', (asset, at, ref, encode(context)))
    return {**frame, 'rotation': context}


def prices(frame, account, policy):
    b, asset = frame['book'], frame['asset_id']
    other = next(o['asset_id'] for o in frame['market']['outcomes'] if o['asset_id'] != asset)
    delta = D(account['positions'].get(asset, '0'))-D(account['positions'].get(other, '0'))
    bid, ask, tick = D(b['bid']), D(b['ask']), D(b['tick_size'])
    mid = (bid+ask)/2
    distance = D(frame['rotation']['distance'])
    # At most half the base distance shifts the center; never erase both edges.
    cap = D(policy.quote_distance)/2
    skew = max(-cap, min(cap, D(policy.inventory_skew)*delta))
    center = mid-skew
    return {'BUY': grid(min(center-distance, ask-tick), tick, 'BUY'),
            'SELL': grid(max(center+distance, bid+tick), tick, 'SELL')}
