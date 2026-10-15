"""Proposed integration regressions for the original repository.

Copy into tests/ and run there. These tests have NOT been run against a complete
checkout in this review environment. They are expected to expose the retention
bug on commit 1a1eb9b; do not weaken the assertions to make the old code pass.
All inputs and funds are synthetic Paper data.
"""
import tempfile
import unittest
from pathlib import Path

from nba_recorder.inplay.demo import AT, frame
from nba_recorder.inplay.manager import UnifiedPaperManager
from nba_recorder.inplay.policy import BookPolicy
from nba_recorder.inplay.reference import book_targets
from nba_recorder.inplay.session import BookSession
from nba_recorder.inplay.store import SingleWriterStore


class RestingOrderRetentionRegression(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SingleWriterStore(Path(self.tmp.name) / 'paper.sqlite')
        self.om = UnifiedPaperManager(self.store, BookPolicy(), AT)
        self.session = BookSession(self.om, 'L1')
        self.session.observe(frame(AT))
        self.f = self.session.observe(frame(AT + 1))
        self.om.split(self.f['market'], '5', 'explicit-synthetic-split')

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_stable_book_retains_existing_sell_without_reserving_twice(self):
        self.session.quote(self.f, 'first-quote')
        self.om.advance(self.om.now + 5)
        sell = next(o for o in self.om.orders() if o['side'] == 'SELL')
        self.assertEqual(sell['state'], 'OPEN')
        fresh = self.session.observe(frame(self.om.now + 1))
        self.session.quote(fresh, 'same-economics-new-observation')
        current = self.om.order(sell['id'])
        self.assertEqual(current['state'], 'OPEN',
                         'An unchanged valid resting SELL must not be cancelled because its own shares are reserved.')
        self.assertTrue(self.om.consistent())

    def test_reduce_only_retains_its_existing_risk_exit(self):
        # Acquire five additional a shares through the Paper OMS, not by editing
        # either ledger: a=10, b=5, and one five-share risk exit is sufficient.
        buy = next(t for t in book_targets(self.f, self.om.account(), [], {}, self.om.policy)['targets'] if t['side'] == 'BUY')
        order_id = self.om.enqueue('paper-bootstrap-buy', buy, self.f)
        self.om.dispatch()
        self.om.advance(self.om.now + 5)
        self.om.fill(order_id, '5', 'synthetic-bootstrap-fill')
        self.om.advance(self.om.now)
        self.assertEqual(self.om.order(order_id)['state'], 'FILLED')
        reduce_frame = self.session.observe(frame(self.om.now + 1, period=4, remaining=120))
        self.session.quote(reduce_frame, 'first-reduce-quote')
        self.om.advance(self.om.now + 5)
        exit_order = next(o for o in self.om.orders() if o.get('purpose') == 'RISK_EXIT' and o['side'] == 'SELL')
        self.assertEqual(exit_order['state'], 'OPEN')
        fresh = self.session.observe(frame(self.om.now + 1, period=4, remaining=120))
        self.session.quote(fresh, 'retain-reduce-quote')
        self.assertEqual(self.om.order(exit_order['id'])['state'], 'OPEN',
                         'An existing reduction order consumes capacity but must not erase itself from the desired portfolio.')
        self.assertTrue(self.om.consistent())


if __name__ == '__main__':
    unittest.main(verbosity=2)
