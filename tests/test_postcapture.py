import asyncio
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock,patch

from nba_recorder.inplay.store import SingleWriterStore
from nba_recorder.inplay.manager import UnifiedPaperManager
from nba_recorder.inplay.session import BookSession
from nba_recorder.inplay.demo import events
from nba_recorder.research.daily import review_session,review_day,publish

SID='d'*32
DAY='2026-10-21'


def fixture(root,finished=True,origin='SYNTHETIC_DATA',gap=False):
    folder=root/'data/console';folder.mkdir(parents=True)
    path=folder/(SID+'.sqlite');store=SingleWriterStore(path)
    om=UnifiedPaperManager(store,start_ms=events()[0]['time_ms']);session=BookSession(om,'L1')
    with om.db:om.db.execute('INSERT INTO ip_session_config VALUES(?,?)',('console_origin',json.dumps({'data_source':origin,'execution_source':'PAPER_EXECUTION'})))
    stream=copy.deepcopy(events())
    if gap:
        for e in stream[1:]:
            e['kind']='WATCHDOG'
            for f in e['frames']:f['book_received_ms']=stream[0]['time_ms']
    for e in stream:session.on_event(e)
    om.kill('fixture_end');om.advance(om.now+200);om.reconcile()
    if finished:
        with om.db:om._log('CONSOLE_SESSION_FINISHED',detail={'fixture':True})
    store.close()
    state=root/'data/unattended'/DAY/'state.json';state.parent.mkdir(parents=True)
    state.write_text(json.dumps({'games':{'123':{'session_id':SID,'status':'FINISHED' if finished else 'RUNNING'}},'discovery_status':'FIXTURE'}))
    return path


class PostcaptureTests(unittest.TestCase):
    def test_actual_child_generates_report_without_parent_writer(self):
        from nba_recorder.unattended import postcapture
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);fixture(root)
            self.assertEqual(asyncio.run(postcapture(root,DAY)),0)
            day=json.loads((root/'data/postcapture/days'/DAY/'latest.json').read_text(encoding='utf-8'))
            self.assertEqual(day['status'],'REVIEWS_AVAILABLE')
            report=day['sessions'][0]['review']
            self.assertTrue((root/report['path']).is_file())

    def test_frozen_idempotent_replay_and_source_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);path=fixture(root);before=hashlib.sha256(path.read_bytes()).hexdigest()
            first=review_session(root,SID);second=review_session(root,SID)
            self.assertEqual(first['id'],second['id']);self.assertEqual(second['status'],'EXISTS')
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),before)
            audit=first['audit'];self.assertIn('NOT_REAL_OBSERVED',audit['reasons'])
            self.assertFalse(audit['full_game_verified']);self.assertEqual(audit['real_submission_calls'],0)
            self.assertTrue(all(c['event_replay_equals_cache'] and c['cash_positions_equal_oms'] for c in audit['ledger_checks']))
            saved=json.loads((root/first['path']).read_text(encoding='utf-8'))
            self.assertIsNotNone(saved['analysis']);self.assertFalse(saved['live_go'])
            horizons={m['seconds'] for rows in audit['markouts'].values() for m in rows}
            if horizons:self.assertEqual(horizons,{10,30,60,300})
            one=review_day(root,DAY);two=review_day(root,DAY)
            self.assertEqual(one,two)
            self.assertEqual(len(list((root/'data/postcapture/days'/DAY).glob('*.json'))),2)

    def test_watchdog_gap_not_new_observations_and_unfinished_no_analysis(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);fixture(root,finished=False,gap=True)
            result=review_session(root,SID);audit=result['audit']
            self.assertIn('SESSION_NOT_FINISHED',audit['reasons']);self.assertIn('DATA_GAPS',audit['reasons'])
            self.assertGreater(audit['watchdog_callbacks'],0)
            self.assertTrue(all(c['unique_book_observations']==1 for c in audit['coverage'].values()))
            self.assertIsNone(json.loads((root/result['path']).read_text(encoding='utf-8'))['analysis'])
            self.assertEqual(review_day(root,DAY)['sessions'][0]['review_status'],'WAITING_FOR_RECORDING_END')

    def test_cache_corruption_detected_and_old_report_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);path=fixture(root)
            first=review_session(root,SID)
            import sqlite3
            from contextlib import closing
            with closing(sqlite3.connect(path)) as db:
                state=json.loads(db.execute('SELECT payload FROM l6_accounts').fetchone()[0]);state['cash']='999'
                db.execute('UPDATE l6_accounts SET payload=?',(json.dumps(state),))
                db.commit()
            changed=review_session(root,SID)
            self.assertNotEqual(first['id'],changed['id'])
            self.assertIn('LEDGER_MISMATCH_OR_MISSING',changed['audit']['reasons'])
            self.assertTrue((root/first['path']).exists())

    def test_no_games_and_atomic_immutable_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            result=review_day(root,DAY);self.assertEqual(result['status'],'NO_RECORDED_SESSIONS')
            path=root/'report.json';publish(path,{'a':1});publish(path,{'a':2})
            self.assertEqual(json.loads(path.read_text()),{'a':1})
            with self.assertRaises(ValueError):review_day(root,'../../secret')

    def test_postcapture_uses_independent_process_not_live_writer(self):
        from nba_recorder.unattended import postcapture
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'data/unattended'/DAY).mkdir(parents=True)
            process=type('Process',(),{'wait':AsyncMock(return_value=0)})()
            with patch('asyncio.create_subprocess_exec',AsyncMock(return_value=process)) as spawn:
                self.assertEqual(asyncio.run(postcapture(root,DAY)),0)
                args=spawn.call_args.args
                self.assertIn('nba_recorder.research.daily',args)
                self.assertNotIn('run_paper',args)

    def test_scheduler_final_consolidation_without_any_orders(self):
        from nba_recorder.unattended import run,bounds
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);end=bounds(DAY)[1]
            with patch('nba_recorder.unattended.utc_ms',return_value=end+7*3600000),patch('nba_recorder.unattended.postcapture',AsyncMock(return_value=0)) as worker:
                asyncio.run(run(DAY,root))
                worker.assert_awaited_once_with(root,DAY)
            state=json.loads((root/'data/unattended'/DAY/'state.json').read_text(encoding='utf-8'))
            self.assertEqual(state['scheduler_status'],'COMPLETE');self.assertEqual(state['postcapture_status'],'FINISHED')
            self.assertEqual(state['real_submission_calls'],0)
