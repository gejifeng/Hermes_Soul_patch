import tempfile
import unittest
from hermes_soul.store import Store, BudgetExhausted

class BudgetResetTests(unittest.TestCase):
    def test_reset_preserves_calls_and_persists_only_today(self):
        with tempfile.TemporaryDirectory() as home:
            s=Store(home)
            try:
                s.reserve_call('attention',1)
                with self.assertRaises(BudgetExhausted):s.reserve_call('attention',1)
                self.assertEqual(s.reset_budget()['used'],0)
                s.close();s=Store(home)
                s.reserve_call('attention',1)
                self.assertEqual(s.budget_usage()['used'],1)
                self.assertEqual(s.budget_usage()['total_recorded'],2)
                with self.assertRaises(BudgetExhausted):s.reserve_call('attention',1)
                s.reserve_call('chat',1,True)
                self.assertEqual(s.budget_usage()['used'],1)
                day=s.budget_usage()['day']
                s.db.execute('UPDATE meta SET key=? WHERE key=?',('budget_baseline:2000-01-01','budget_baseline:'+day))
                self.assertEqual(s.budget_usage()['used'],2)
            finally:s.close()
