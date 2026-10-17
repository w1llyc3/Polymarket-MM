"""Versioned local Paper plans. No credentials, account resets or execution toggle."""
import json
import re
import uuid
from dataclasses import replace
from pathlib import Path
from ..api import utc_ms
from ..fair_value.maths import decimal
from ..inplay.policy import BookPolicy

FIELDS={'quote_distance','order_size','max_order_shares','max_order_notional','max_event_risk',
        'max_account_risk','max_asset_inventory','max_one_sided_shares','max_order_lifetime_ms',
        'inventory_skew','min_top_depth','reduce_remaining_s','close_remaining_s','max_daily_loss'}
INTEGER={'max_order_lifetime_ms','reduce_remaining_s','close_remaining_s'}


def validate(parameters):
    if not isinstance(parameters,dict) or set(parameters)-FIELDS:raise ValueError('unsupported Paper plan parameters')
    p={k:int(v) if k in INTEGER and type(v) is int else str(v) for k,v in parameters.items()}
    if any(type(p[k]) is not int for k in INTEGER & p.keys()):raise ValueError('integer plan field required')
    policy=replace(BookPolicy(),**p)
    for k in FIELDS-INTEGER:
        value=decimal(getattr(policy,k))
        if value<0 or value>10000:raise ValueError('bounded nonnegative plan values required')
    if decimal(policy.order_size)<=0 or decimal(policy.order_size)>decimal(policy.max_order_shares) or decimal(policy.max_one_sided_shares)>decimal(policy.max_asset_inventory) or decimal(policy.max_event_risk)>decimal(policy.max_account_risk):
        raise ValueError('inconsistent size/inventory/risk caps')
    return p


def listing(root):
    entries=[{'id':k,'version':1,'name':n,'parameters':{'quote_distance':d},'builtin':True}
             for k,n,d in [('SMALL','小额研究','.02'),('WIDE','宽距研究','.03')]]
    folder=Path(root)/'plans'
    if folder.is_symlink():raise ValueError('plan directory must be local')
    if folder.exists():
        for file in sorted(folder.glob('*.json')):
            if file.is_symlink() or not re.fullmatch('[0-9a-f]{32}-[0-9]+',file.stem):continue
            value=json.loads(file.read_text(encoding='utf-8'));validate(value['parameters']);entries.append(value)
    return entries


def load(root,identity,version):
    if type(version) is not int:raise ValueError('plan version required')
    item=next((p for p in listing(root) if p['id']==identity and p['version']==version),None)
    if not item:raise ValueError('unknown registered plan/version')
    return item


def save(root,name,parameters,identity=None,expected_version=0):
    if not isinstance(name,str) or not 1<=len(name.strip())<=60:raise ValueError('bounded plan name required')
    values=validate(parameters);entries=listing(root)
    if len(entries)>=202:raise ValueError('plan version limit 200')
    if identity is None:
        identity=uuid.uuid4().hex
        if expected_version!=0:raise ValueError('new plan starts at zero')
    elif not re.fullmatch('[0-9a-f]{32}',identity):raise ValueError('invalid plan ID')
    latest=max((p['version'] for p in entries if p['id']==identity),default=0)
    if type(expected_version) is not int or expected_version!=latest:raise ValueError('plan changed; reload before saving')
    value={'id':identity,'version':latest+1,'name':name.strip(),'parameters':values,'created_ms':utc_ms(),'builtin':False,'mode':'PAPER'}
    folder=Path(root)/'plans';folder.mkdir(exist_ok=True)
    if folder.is_symlink():raise ValueError('plan directory must be local')
    temporary=folder/(uuid.uuid4().hex+'.tmp')
    try:
        with temporary.open('x',encoding='utf-8') as f:json.dump(value,f,ensure_ascii=False,indent=2)
        __import__('os').link(temporary,folder/(identity+'-'+str(latest+1)+'.json'))
    finally:temporary.unlink(missing_ok=True)
    return value
