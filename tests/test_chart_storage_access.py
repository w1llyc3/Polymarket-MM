import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from nba_recorder.console.service import ConsoleService
from nba_recorder.console.chart import query
from nba_recorder.inplay.demo import events, AT
from nba_recorder.inplay.store import SingleWriterStore
from nba_recorder.inplay.manager import UnifiedPaperManager
from nba_recorder.inplay.session import BookSession


class HistoricalChartAccessTests(unittest.TestCase):
    def test_chart_uses_recorded_outcomes_without_whole_history_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            service=ConsoleService(tmp);sid='a'*32;path=service.data/(sid+'.sqlite')
            store=SingleWriterStore(path)
            try:
                om=UnifiedPaperManager(store,start_ms=AT)
                with store.db:
                    store.db.execute('INSERT INTO ip_session_config VALUES(?,?)',('console_origin',json.dumps({'data_source':'SYNTHETIC_DATA'})))
                BookSession(om,'L2').on_event(events()[0])
            finally:store.close()
            params={'session_id':sid,'asset_id':'a','from':str(AT),'to':str(AT+1000)}
            with patch('nba_recorder.console.history.view',side_effect=AssertionError('must not scan history')):
                actual=service.chart_query(params)
                self.assertEqual(actual,query(path,'a',AT,AT+1000,{'input':0,'journal':0,'phase':0,'game':0}))
                with self.assertRaisesRegex(ValueError,'unknown outcome'):
                    service.chart_query({**params,'asset_id':'not-recorded'})


if __name__ == '__main__':unittest.main()
