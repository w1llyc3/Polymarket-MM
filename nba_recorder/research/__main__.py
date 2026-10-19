import argparse
import json
from pathlib import Path
from .catalog import catalog
from .engine import experiment


def main():
    p=argparse.ArgumentParser(description='Offline sample catalog, immutable replay and Paper comparison; no live mode')
    p.add_argument('command',choices=['catalog','run','register'])
    p.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[2])
    p.add_argument('--sample')
    p.add_argument('--synthetic',action='store_true')
    p.add_argument('--out',type=Path)
    p.add_argument('--distances',default='.01,.02,.03')
    p.add_argument('--models',default='L0,L1,L2')
    p.add_argument('--budget',default='100')
    p.add_argument('--split-shares',default='0',help='Explicit collateral-funded Paper split, not free inventory')
    p.add_argument('--sensitivity',action='store_true',help='Queue hypotheses .5/1/2; not calibrated probabilities')
    p.add_argument('--profiles',type=Path,help='JSON profiles: name, parameters, queue_multiplier, latencies')
    p.add_argument('--seconds',type=int,help='Explicit first N seconds of recorded sample; omitted means entire sample')
    p.add_argument('--synthetic-seconds',type=int,help='Explicit stretched synthetic load fixture, not a real game')
    p.add_argument('--cadence-ms',type=int,default=1000)
    a=p.parse_args()
    if a.command=='catalog':result=catalog(a.root)
    elif a.command=='register':
        if not a.out:p.error('--out required: finished data/research/<32-hex-id>')
        from .jobs import register
        result=register(a.root,a.out)
    else:
        if not a.out:p.error('--out required, choose a new directory')
        scenarios=[{'name':name,'parameters':{},'queue_multiplier':q} for name,q in [('optimistic','.5'),('baseline','1'),('conservative','2')]] if a.sensitivity else None
        if a.profiles:scenarios=json.loads(a.profiles.read_text(encoding='utf-8'))
        result=experiment(a.root,a.out,a.sample,a.synthetic,a.distances.split(','),a.models.split(','),a.budget,a.split_shares,scenarios,
            seconds=a.seconds,synthetic_seconds=a.synthetic_seconds,cadence_ms=a.cadence_ms)
        result={k:v for k,v in result.items() if k not in {'lanes','manifest'}}
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
