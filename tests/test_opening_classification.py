import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from nba_recorder.api import select_market
from nba_recorder.discovery import normalize_market
from nba_recorder.game_state import VERIFIED_GAMES,schedule_match
from nba_recorder.unattended import paper_allowed,Control
from tests.test_core import fixture


class OpeningClassificationTests(unittest.TestCase):
    def market(self):
        e,m,_=fixture();e['gameId']='synthetic-sports-id'
        m['description']='final score including any overtime postponed canceled 50-50'
        return e,m

    def test_individual_verified_games_exact_roles_and_timestamp(self):
        rows=[r for r in VERIFIED_GAMES if r['season_type']=='REGULAR_SEASON']
        self.assertEqual(len(rows),3)
        for r in rows:
            self.assertEqual(schedule_match(r['away_team'],r['home_team'],r['start_ms'])['nba_game_id'],r['nba_game_id'])
            self.assertIsNone(schedule_match(r['away_team'],r['home_team'],r['start_ms']+1))
            self.assertIsNone(schedule_match(r['home_team'],r['away_team'],r['start_ms']))

    def test_general_and_legacy_paths_agree_regular_not_preseason(self):
        e,m=self.market()
        general,_=normalize_market(e,m,{'series':'10345'},0)
        legacy,_=select_market(e,m,'10345',0)
        self.assertEqual(general['season_type'],'REGULAR_SEASON')
        self.assertEqual(legacy['season_type'],'REGULAR_SEASON')
        self.assertEqual(general['nba_game_id'],'0022600001')
        self.assertTrue(paper_allowed(general));self.assertEqual(Control(general).mode,'REAL_PAPER')
        e['seasonType']='Preseason'
        conflict,_=normalize_market(e,m,{'series':'10345'},0)
        self.assertEqual(conflict['season_type'],'UNKNOWN');self.assertTrue(conflict['classification_conflict'])
        self.assertFalse(paper_allowed(conflict))
        from nba_recorder.inplay.demo import frame
        from nba_recorder.inplay.reference import book_risk
        from nba_recorder.inplay.policy import BookPolicy
        f=frame();f['market']={**f['market'],'classification_conflict':True,'research_authorized':True}
        self.assertIn('conflicting_competition_classification',book_risk(f,BookPolicy(research_phase=True),f['time_ms']))

    def test_time_change_and_bad_official_binding_never_get_cached_permission(self):
        e,m=self.market();e['startTime']='2026-10-20T19:01:00Z'
        moved,_=normalize_market(e,m,{'series':'10345'},0)
        self.assertEqual(moved['season_type'],'UNKNOWN');self.assertFalse(paper_allowed(moved))
        official=copy.deepcopy(VERIFIED_GAMES[1])
        bad,_=normalize_market(e,m,{'series':'10345'},0,official)
        self.assertTrue(bad['identity_conflict']);self.assertFalse(bad['collection_allowed'])

    def test_controlled_paper_quotes_without_granting_pregame_phase(self):
        from nba_recorder.inplay.store import SingleWriterStore
        from nba_recorder.inplay.manager import UnifiedPaperManager
        from nba_recorder.inplay.policy import BookPolicy
        from nba_recorder.inplay.runner import PaperRunner
        from nba_recorder.inplay.demo import frame
        from nba_recorder.inplay.sports import normalize
        e,m=self.market();market,_=normalize_market(e,m,{'series':'10345'},0)
        at=market['game_start_ms']+600000
        with tempfile.TemporaryDirectory() as directory:
            store=SingleWriterStore(Path(directory)/'paper.sqlite')
            try:
                om=UnifiedPaperManager(store,BookPolicy(research_phase=True,initial_cash='25'),at)
                runner=PaperRunner(om,'L2')
                with om.db:om.db.execute('INSERT INTO ip_session_config VALUES(?,?)',('console_origin',json.dumps({'data_source':'SYNTHETIC_DATA'})))
                def feed(t,live):
                    frames=[]
                    for asset in ('a','b'):
                        f=frame(t,asset);f['market']={**market,'event_live':live,'research_authorized':True}
                        f['market_id']=market['market_id'];f['book']['market_id']=market['market_id']
                        f['sports']=normalize({'gameId':e['gameId'],'leagueAbbreviation':'NBA','homeTeam':'det','awayTeam':'bos',
                            'status':'InProgress' if live else 'Scheduled','live':live,'ended':False,'period':'Q2' if live else ''},t,
                            {'game_id':e['gameId'],'event_id':market['event_id'],'home_team':'det','away_team':'bos','identity_evidence':'SYNTHETIC_PHASE_TEST_ONLY'})
                        frames.append(f)
                    with patch('nba_recorder.inplay.runner.utc_ms',return_value=t):runner.feed(frames,provenance='SYNTHETIC_PHASE_TEST_ONLY')
                feed(at,False)
                self.assertEqual(om.orders(),[])
                feed(at+1,True);om.advance(at+10)
                self.assertGreater(len(om.orders()),0)
                self.assertTrue(any((om.adapter.authoritative(o['id']) or {}).get('state')=='OPEN' for o in om.orders()))
                self.assertEqual(om.real_submission_calls,0)
            finally:store.close()

    def test_pending_reservation_refreshes_evidence_without_new_session_or_orders(self):
        import asyncio
        from unittest.mock import AsyncMock
        from nba_recorder.unattended import run
        e,m=self.market();market,_=normalize_market(e,m,{'series':'10345'},0)
        async def exercise(root):
            unknown={**market,'season_type':'UNKNOWN','nba_game_id':None}
            scan={'status':'MARKET_FOUND','markets':[unknown],'schedule_only':[]}
            with patch('nba_recorder.unattended.discover',AsyncMock(return_value=scan)),patch('nba_recorder.unattended.utc_ms',return_value=market['game_start_ms']-86400000),patch('nba_recorder.unattended.run_paper',AsyncMock()) as runner:
                await run('2026-10-21',root,once=True)
                path=root/'data/unattended/2026-10-21/state.json'
                first=json.loads(path.read_text(encoding='utf-8'))
                scan['markets']=[market]
                await run('2026-10-21',root,once=True)
                second=json.loads(path.read_text(encoding='utf-8'))
                before=first['games'][market['market_id']];after=second['games'][market['market_id']]
                self.assertEqual(before['session_id'],after['session_id'])
                self.assertEqual(after['market']['season_type'],'REGULAR_SEASON')
                self.assertTrue(paper_allowed(after['market']));runner.assert_not_called()
        with tempfile.TemporaryDirectory() as directory:asyncio.run(exercise(Path(directory)))
