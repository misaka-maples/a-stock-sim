import json
from pathlib import Path
import tempfile
import threading
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from market_request_budget import (MarketRequestBudget, RequestBudgetExceeded,
                                   RequestBudgetUnavailable, RequestQueueBusy)


class FakeClock:
    def __init__(self):
        self.now = 1792022400.  # fixed date, never invokes a real data source
    def time(self):
        return self.now
    def sleep(self, seconds):
        self.now += seconds


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = FakeClock()
        self.budget = self.make()

    def make(self, **options):
        return MarketRequestBudget(self.tmp.name, clock=self.clock.time,
                                   monotonic=self.clock.time, sleep=self.clock.sleep,
                                   block_untracked_baostock_day=False, **options)

    def test_failed_requests_count_and_restart_preserves_count(self):
        with self.assertRaises(OSError):
            with self.budget.request('baostock'):
                raise OSError('transport failed')
        with self.make().request('baostock'):
            pass
        state = self.make().status('baostock')
        self.assertEqual(state['used'], 2)
        self.assertEqual(state['remaining'], 39998)

    def test_daily_limit_stops_before_network_and_rolls_beijing_midnight(self):
        budget = self.make(policies={'baostock': {'daily_limit': 2}})
        for _ in range(2):
            with budget.request('baostock'):
                pass
        with self.assertRaises(RequestBudgetExceeded):
            with budget.request('baostock'):
                self.fail('admitted over cap')
        self.clock.now = budget._tomorrow(self.clock.now)+.1
        with budget.request('baostock'):
            pass
        self.assertEqual(budget.status('baostock')['used'], 1)

    def test_reservation_persisted_before_io_even_on_crash(self):
        with self.budget.request('tushare'):
            state = json.loads((Path(self.tmp.name)/'tushare.json').read_text())
            self.assertEqual(state['used'], 1)

    def test_failure_slows_and_success_gradually_recovers(self):
        for _ in range(3):
            with self.assertRaises(RuntimeError):
                with self.budget.request('eastmoney'):
                    raise RuntimeError()
        self.assertEqual(self.budget.status('eastmoney')['interval'], 8.)
        with self.budget.request('eastmoney'):
            pass
        self.assertAlmostEqual(self.budget.status('eastmoney')['interval'], 6.4)

    def test_providers_have_independent_quotas(self):
        with self.budget.request('tencent'):
            pass
        self.assertEqual(self.budget.status('eastmoney')['used'], 0)

    def test_untracked_baostock_day_fails_closed_until_next_day(self):
        budget = MarketRequestBudget(self.tmp.name, clock=self.clock.time,
                                     monotonic=self.clock.time, sleep=self.clock.sleep)
        with self.assertRaises(RequestBudgetExceeded) as caught:
            with budget.request('baostock'):
                self.fail('untracked previous requests')
        self.assertEqual(caught.exception.code, 'untracked_prior_usage')
        self.assertFalse(budget.status('baostock')['usage_known'])
        self.clock.now = budget._tomorrow(self.clock.now)
        with budget.request('baostock'):
            pass
        self.assertTrue(budget.status('baostock')['usage_known'])

    def test_corrupt_or_unwritable_state_never_admits_network(self):
        path = Path(self.tmp.name)/'baostock.json'
        path.write_text('{}')
        with self.assertRaises(RequestBudgetUnavailable):
            with self.budget.request('baostock'):
                self.fail('corrupt state')
        path.unlink()
        with patch('market_request_budget.os.replace', side_effect=OSError('disk full')):
            with self.assertRaises(RequestBudgetUnavailable):
                with self.budget.request('baostock'):
                    self.fail('reservation not durable')

    def test_bounded_rate_wait_does_not_consume_budget(self):
        budget = self.make(max_wait=.1)
        with budget.request('tencent'):
            pass
        with self.assertRaises(RequestQueueBusy):
            with budget.request('tencent'):
                self.fail('rate too high')
        self.assertEqual(budget.status('tencent')['used'], 1)

    def test_two_instances_share_serial_lock_and_bound_queue(self):
        first = MarketRequestBudget(self.tmp.name, max_wait=.1)
        second = MarketRequestBudget(self.tmp.name, max_wait=.05)
        errors = []
        with first.request('tencent'):
            thread = threading.Thread(target=lambda: self._attempt(second, errors))
            thread.start()
            thread.join(1)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], RequestQueueBusy)
        self.assertEqual(first.status('tencent')['used'], 1)

    @staticmethod
    def _attempt(budget, errors):
        try:
            with budget.request('tencent'):
                errors.append(AssertionError('overlap'))
        except Exception as exc:
            errors.append(exc)

    def test_cannot_raise_caps_or_set_nonfinite_policy(self):
        for policy in ({'daily_limit': 50000}, {'min_interval': float('nan')}):
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                self.make(policies={'baostock': policy}).status('baostock')

    def test_other_process_cannot_overlap_request(self):
        budget = MarketRequestBudget(self.tmp.name, max_wait=.1)
        child = '''
import sys
from market_request_budget import MarketRequestBudget, RequestQueueBusy
try:
    with MarketRequestBudget(sys.argv[1], max_wait=.05).request('tencent'):
        sys.exit(2)
except RequestQueueBusy:
    sys.exit(0)
'''
        with budget.request('tencent'):
            result = subprocess.run([sys.executable, '-c', child, self.tmp.name],
                                    capture_output=True, timeout=5,
                                    cwd=Path(__file__).resolve().parent)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(budget.status('tencent')['used'], 1)

    def test_status_is_nonblocking_while_request_holds_lock(self):
        first = MarketRequestBudget(self.tmp.name, max_wait=15)
        second = MarketRequestBudget(self.tmp.name, max_wait=15)
        with first.request('tencent'):
            started = time.monotonic()
            snapshot = second.status('tencent')
            self.assertLess(time.monotonic()-started, .2)
            self.assertEqual(snapshot['used'], 1)

    def test_status_missing_state_and_rollover_never_writes(self):
        budget = MarketRequestBudget(self.tmp.name, clock=self.clock.time)
        with patch.object(budget, '_save', side_effect=AssertionError('status wrote')):
            self.assertEqual(budget.status('baostock')['reason'], 'untracked_prior_usage')
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])
        with self.budget.request('tencent'):
            pass
        self.clock.now = self.budget._tomorrow(self.clock.now)
        with patch.object(self.budget, '_save', side_effect=AssertionError('rollover wrote')):
            self.assertEqual(self.budget.status('tencent')['used'], 0)

    def test_inner_local_admission_refusal_is_not_network_failure(self):
        with self.assertRaises(RequestQueueBusy):
            with self.budget.request('baostock'):
                raise RequestQueueBusy('queue_busy', 'baostock')
        state = self.budget.status('baostock')
        self.assertEqual(state['used'], 1)
        self.assertEqual(state['failures'], 0)
        self.assertEqual(state['interval'], 1)

    def test_initialize_records_unknown_day_without_network_reservation(self):
        budget = MarketRequestBudget(self.tmp.name, clock=self.clock.time,
                                     monotonic=self.clock.time, sleep=self.clock.sleep)
        first = budget.initialize('baostock')
        self.assertEqual(first['reason'], 'untracked_prior_usage')
        self.assertEqual(first['used'], 0)
        self.assertGreater(first['retry_after'], 0)
        self.assertTrue((Path(self.tmp.name)/'baostock.json').is_file())
        self.clock.now = budget._tomorrow(self.clock.now)
        second = budget.initialize('baostock')
        self.assertTrue(second['usage_known'])
        self.assertEqual(second['reason'], '')
        self.assertEqual(second['remaining'], 40000)
        self.assertEqual(second['used'], 0)


if __name__ == '__main__':
    unittest.main()
