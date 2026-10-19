"""Bounded, reproducible evidence from a completed synthetic scale experiment."""
import argparse
import hashlib
import json
import sqlite3
from decimal import Decimal
from pathlib import Path


def summarize(directory):
    directory=Path(directory)
    comparison=json.loads((directory/'comparison.json').read_text(encoding='utf-8'))
    assert comparison['sample']['source']=='SYNTHETIC_DATA'
    assert comparison['real_submission_calls']==0 and comparison['live_go'] is False
    rows=[]
    for lane in comparison['lanes']:
        folder=directory/lane['lane']
        with sqlite3.connect((folder/'paper.sqlite').resolve().as_uri()+'?mode=ro',uri=True) as db:
            digest=hashlib.sha256();count=0
            for time_ms,payload in db.execute('SELECT time_ms,payload FROM ip_inputs ORDER BY time_ms,rowid'):
                digest.update((str(time_ms)+'\t'+payload+'\n').encode());count+=1
            clock=db.execute('SELECT min(time_ms),max(time_ms) FROM ip_inputs').fetchone()
        account=lane['metrics']['account']
        assert lane['shutdown_ledger_consistent'] and account['reconciled']
        assert lane['tape_id']==comparison['tape_id'] and lane['real_submission_calls']==0
        assert Decimal(account['equity'])==Decimal(account['cash'])+Decimal(account['market_value'])
        assert Decimal(account['net_pnl'])==Decimal(account['equity'])-Decimal(account['capital_contributions'])
        rows.append({'lane':lane['lane'],'inputs':count,'input_sequence_sha256':digest.hexdigest(),
            'span_ms':clock[1]-clock[0],'frame_count':lane['metrics']['frame_count'],
            'orders':lane['metrics']['orders'],'fill_events':lane['metrics']['fill_events'],
            'account':account,'fill_markouts':lane['metrics']['markout_summary'],
            'performance':lane['performance'],'ledger_consistent':True})
    assert len({r['input_sequence_sha256'] for r in rows})==1
    return {'source':'SYNTHETIC_SCALE_FIXTURE_NOT_REAL_GAME','code_commit':comparison['code_commit'],
        'working_tree_dirty':comparison['working_tree_dirty'],'tape_id':comparison['tape_id'],
        'performance':comparison['performance'],'lanes':rows,'same_input_sequence_verified':True,
        'equity_cash_and_pnl_identities_verified':True,'real_submission_calls':0,'live_go':False}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('directory',type=Path);parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args()
    args.out.write_text(json.dumps(summarize(args.directory),ensure_ascii=False,indent=2),encoding='utf-8')
