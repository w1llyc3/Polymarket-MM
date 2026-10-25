import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock
from nba_recorder.unattended import bounds, due, eligible, paper_allowed, run, Control
from nba_recorder.pilot import PilotWindow


class UnattendedTests(unittest.TestCase):
    def test_half_hour_record_only_window(self):
        start,end=bounds('2026-10-19');m=self.market();tip=m['game_start_ms']
        self.assertFalse(due(m,tip-1800001,start,end,1800))
        self.assertTrue(due(m,tip-1800000,start,end,1800))
        self.assertEqual(Control(m,record_only=True).mode,'OBSERVATION')
        w=PilotWindow(tip-1800000,28800,1800)
        finished=tip+7200000
        w.observe({'display_eligible':True,'game_finished':True,'received_ms':finished},finished)
        self.assertIsNone(w.stop_reason(finished+1799999))
        self.assertEqual(w.stop_reason(finished+1800000),'OBSERVED_GAME_END_PLUS_TAIL')

    def test_observed_end_is_persisted_without_extending_tail_on_repeated_messages(self):
        from nba_recorder.inplay.store import SingleWriterStore
        from nba_recorder.inplay.manager import UnifiedPaperManager
        from nba_recorder.inplay.runner import PaperRunner
        with tempfile.TemporaryDirectory() as directory:
            store=SingleWriterStore(Path(directory)/'paper.sqlite')
            try:
                om=UnifiedPaperManager(store,start_ms=1000);runner=PaperRunner(om,'L2');w=PilotWindow(1000,28800,1800)
                for at in (2000,3000):runner.observe_game_information({'display_eligible':True,'game_finished':True,'received_ms':at},w,at)
                self.assertEqual(w.finished_ms,2000)
                self.assertEqual(int(om.db.execute("SELECT payload FROM ip_session_config WHERE id='recording_finished_ms'").fetchone()[0]),2000)
                self.assertEqual(om.real_submission_calls,0)
            finally:store.close()

    def market(self):
        start, _ = bounds('2026-10-19')
        return {'market_id': '123', 'slug': 'nba-test', 'venue': 'polymarket.com',
                'collection_allowed': True, 'outcomes': [1, 2], 'game_start_ms': start+8*3600000,
                'research_eligible': True, 'rules_verified': True, 'season_type': 'PRESEASON'}

    def test_beijing_day_and_pregame_window(self):
        start, end = bounds('2026-10-19')
        self.assertEqual(end-start, 86400000)
        self.assertEqual(start, 1792339200000)
        m = self.market(); tip = m['game_start_ms']
        self.assertFalse(due(m, tip-3600001, start, end))
        self.assertTrue(due(m, tip-3600000, start, end))
        self.assertFalse(due(m, tip+7*3600000, start, end))
        self.assertFalse(due({**m, 'game_start_ms': end}, end, start, end))

    def test_no_us_unknown_or_unverified_paper(self):
        m = self.market()
        self.assertTrue(paper_allowed(m))
        self.assertFalse(eligible({**m, 'venue': 'polymarket.us'}))
        for field, value in [('season_type', 'UNKNOWN'), ('rules_verified', False), ('identity_conflict', True)]:
            self.assertFalse(paper_allowed({**m, field: value}))
        self.assertEqual(Control({**m, 'season_type': 'UNKNOWN'}).mode, 'OBSERVATION')
        self.assertEqual(Control(m).paper_budget, '25')

    def test_one_hour_tail_requires_observed_end(self):
        w = PilotWindow(0, 28800, 3600)
        self.assertIsNone(w.stop_reason(5*3600000))
        w.observe({'display_eligible': True, 'game_finished': True, 'received_ms': 18000000}, 18000000)
        self.assertIsNone(w.stop_reason(21599999))
        self.assertEqual(w.stop_reason(21600000), 'OBSERVED_GAME_END_PLUS_TAIL')
        self.assertEqual(PilotWindow(0,28800,3600).stop_reason(28800000), 'HARD_LIMIT_NOT_COMPLETE')
        with self.assertRaises(ValueError): PilotWindow(0,28801,3600)
        with self.assertRaises(ValueError): PilotWindow(0,28800,3601)

    def test_preflight_never_runs_or_refunds(self):
        import asyncio,json
        m=self.market()
        async def execute(root):
            with patch('nba_recorder.unattended.discover', AsyncMock(return_value={'status':'MARKET_FOUND','markets':[m],'schedule_only':[]})), \
                 patch('nba_recorder.unattended.utc_ms',return_value=m['game_start_ms']), \
                 patch('nba_recorder.unattended.run_paper',new_callable=AsyncMock) as runner:
                await run('2026-10-19',root,once=True)
                path=root/'data/unattended/2026-10-19/state.json'
                first=json.loads(path.read_text())
                await run('2026-10-19',root,once=True)
                second=json.loads(path.read_text())
                runner.assert_not_called()
                self.assertEqual(first['games']['123']['session_id'],second['games']['123']['session_id'])
                self.assertEqual(second['real_submission_calls'],0)
                self.assertEqual(second['games']['123']['status'],'SCHEDULED')
                summaries=[json.loads(line) for line in (path.parent/'discovery-summary.jsonl').read_text(encoding='utf-8').splitlines()]
                self.assertEqual(len(summaries),2)
                self.assertEqual(summaries[0]['schema'],'DISCOVERY_SUMMARY_V2')
                self.assertFalse(summaries[1]['raw_response_saved'])
                self.assertFalse((path.parent/'discovery.jsonl').exists())
        with tempfile.TemporaryDirectory() as directory: asyncio.run(execute(Path(directory)))
