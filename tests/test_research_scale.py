import json
import tempfile
import unittest
from pathlib import Path

from nba_recorder.research.scale import events, peak_rss_bytes
from nba_recorder.research.engine import experiment


class ScaleTests(unittest.TestCase):
    def test_plot_sampling_preserves_real_segments_not_artificial_gaps(self):
        from nba_recorder.research.jobs import plot
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);identity='b'*32;lane='p0-L0-d0'
            run=root/'data/research'/identity/lane;run.mkdir(parents=True)
            (run.parent.parent/(identity+'.json')).write_text('{}')
            (run/'decisions.jsonl').write_text('');(run/'fills.json').write_text('[]')
            with (run/'timeline.jsonl').open('w') as fp:
                for i in range(4000):
                    frame={'asset_id':'a','connected':i!=2000,'book_received_ms':i*1000,'book':{'bid':'.4','ask':'.6'}}
                    fp.write(json.dumps({'kind':'INPUT','time_ms':i*1000,'frames':[frame]})+'\n')
            result=plot(root,identity,lane)
            self.assertGreater(result['plot_sample_stride'],1)
            self.assertEqual({p['segment'] for p in result['points']},{1,2})
            self.assertEqual(result['total_points'],3999)
            self.assertEqual(result['points'][0]['segment'],result['points'][1]['segment'])

    def test_deterministic_clock_prints_and_explicit_synthetic_source(self):
        first=list(events(600,1000));second=list(events(600,1000))
        self.assertEqual(first,second)
        self.assertEqual(len(first),601)
        self.assertEqual(first[-1]['time_ms']-first[0]['time_ms'],600000)
        prints=[p for e in first for p in e['trades']]
        self.assertEqual(len(prints),10)
        self.assertEqual(len({p['event_id'] for p in prints}),10)
        for e in first:
            self.assertIn('SYNTHETIC',e['provenance'])
            self.assertEqual(len(e['frames']),2)
            self.assertTrue(all(f['time_ms']==e['time_ms'] for f in e['frames']))
            self.assertTrue(all(p['exchange_ms']<=e['time_ms'] for p in e['trades']))
        with self.assertRaises(ValueError):list(events(0))

    def test_replay_performance_and_isolated_ledger_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(__file__).resolve().parents[1]
            output=Path(folder)/'data/research'/('a'*32)
            result=experiment(root,output,synthetic=True,synthetic_seconds=60,
                models=('L0','L1','L2'),distances=('.01',),split_shares='5')
            self.assertEqual(len(result['lanes']),3)
            self.assertEqual(len({r['tape_id'] for r in result['lanes']}),1)
            self.assertFalse(result['live_go'])
            self.assertEqual(result['real_submission_calls'],0)
            for row in result['lanes']:
                self.assertTrue(row['shutdown_ledger_consistent'])
                self.assertGreater(row['performance']['database_bytes'],0)
                self.assertGreater(row['performance']['wall_seconds'],0)
                saved=json.loads((output/row['lane']/'result.json').read_text())
                self.assertEqual(saved['performance'],row['performance'])
            self.assertGreater(peak_rss_bytes(),0)
            from nba_recorder.research.jobs import register,read,plot
            registered=register(folder,output)
            self.assertEqual(read(folder,registered['id'])['status'],'FINISHED')
            self.assertGreater(plot(folder,registered['id'],result['lanes'][0]['lane'])['total_points'],0)
            with self.assertRaises(FileExistsError):register(folder,output)
            with self.assertRaises(ValueError):register(folder,Path(folder))
