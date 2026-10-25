import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from nba_recorder.recording_service import RollingRecorder,recording_days
from nba_recorder.unattended import CST


class Process:
    pid=123
    code=None
    def poll(self):return self.code


class RecordingServiceTests(unittest.TestCase):
    def test_rollover_and_previous_day_tail(self):
        self.assertEqual(recording_days(datetime(2026,10,23,1,tzinfo=CST)),['2026-10-22','2026-10-23','2026-10-24'])
        self.assertEqual(recording_days(datetime(2026,10,23,8,tzinfo=CST)),['2026-10-23','2026-10-24'])

    def test_only_record_only_workers_and_no_duplicate_tick(self):
        with tempfile.TemporaryDirectory() as directory:
            calls=[]
            def spawn(args,label):calls.append((args,label));return Process()
            s=RollingRecorder(directory,spawn,lambda:'HEALTHY');now=datetime(2026,10,23,12,tzinfo=CST)
            s.tick(now);s.tick(now)
            self.assertEqual(len(calls),2)
            for args,_ in calls:
                self.assertIn('--record-only',args)
                self.assertEqual(args[args.index('--pre-minutes')+1],'30')
                self.assertEqual(args[args.index('--post-minutes')+1],'30')
            s.workers['2026-10-23'].code=1;s.tick(now)
            self.assertEqual(len(calls),2)
            s.tick(datetime(2026,10,23,12,3,tzinfo=CST));self.assertEqual(len(calls),3)

    def test_completed_days_and_existing_mode_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for day,state in [('2026-10-23',{'scheduler_status':'COMPLETE'}),('2026-10-24',{'recording_window':{'record_only':False}})]:
                p=root/'data/unattended'/day/'state.json';p.parent.mkdir(parents=True);p.write_text(json.dumps(state))
            calls=[];s=RollingRecorder(root,lambda *a:calls.append(a),lambda:'HEALTHY')
            s.tick(datetime(2026,10,23,12,tzinfo=CST));self.assertFalse(calls)

    def test_console_restart_and_foreign_service_preservation(self):
        with tempfile.TemporaryDirectory() as directory:
            calls=[]
            def spawn(args,label):calls.append(label);return Process()
            s=RollingRecorder(directory,spawn,lambda:'FOREIGN_SERVICE');now=datetime(2026,10,23,12,tzinfo=CST)
            s.tick(now);self.assertNotIn('console',calls)
            s.health=lambda:'UNAVAILABLE';s.tick(now);s.tick(now)
            self.assertEqual(calls.count('console'),1)
