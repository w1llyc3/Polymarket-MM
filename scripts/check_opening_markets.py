"""Real public input smoke test. Isolated Paper accounts, never actual orders."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from nba_recorder.api import PublicAPI
from nba_recorder.discovery import discover
from nba_recorder.unattended import bounds,Control,paper_allowed
from nba_recorder.inplay.runner import run_paper


async def check(day,output,seconds):
    output.mkdir(parents=True,exist_ok=False);api=PublicAPI();start,end=bounds(day)
    try:scan=await discover(api,start,end-1)
    finally:await api.close()
    (output/'discovery.json').write_text(json.dumps(scan,ensure_ascii=False,indent=2),encoding='utf-8')
    async def game(m):
        folder=output/m['market_id'];folder.mkdir()
        try:
            result=await run_paper(folder/'paper.sqlite',folder/'source.sqlite',m['slug'],m['market_id'],
                seconds=seconds,control=Control(m),research=True,postgame_s=0,
                initial_market={**m,'research_authorized':paper_allowed(m)})
            (folder/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
            return {'market_id':m['market_id'],'question':m['question'],'season_type':m['season_type'],
                'game_start':m['game_start'],'nba_game_id':m['nba_game_id'],'paper_eligible':paper_allowed(m),
                'mode':Control(m).mode,'coverage':result['coverage'],'runner':result['runner'],
                'pilot':result['pilot'],'orders':result['orders'],'ledger_consistent':result['ledger_and_oms_consistent'],
                'real_submission_calls':result['real_submission_calls']}
        except Exception as error:return {'market_id':m['market_id'],'error':type(error).__name__,'message':str(error)[:200]}
    results=await asyncio.gather(*(game(m) for m in scan['markets']))
    value={'source':'REAL_PUBLIC_INPUT_ISOLATED_PAPER_SMOKE_NOT_FULL_GAME','day':day,
        'discovery_status':scan['status'],'evidence':scan['evidence'],'results':results,
        'live_go':False,'real_submission_calls':0,'limitation':'Pregame NO_QUOTE is expected; cannot prove future sports delivery or actual fills'}
    (output/'summary.json').write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    return value


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--date',default='2026-10-21');parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--seconds',type=int,default=30);args=parser.parse_args()
    if not 10<=args.seconds<=120:parser.error('10..120 seconds required')
    result=asyncio.run(check(args.date,args.out,args.seconds))
    print(json.dumps(result,ensure_ascii=False,indent=2))
