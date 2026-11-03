"""Status descriptions must not invent activity or candidate observation."""
import copy
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from rules_v3 import TZ
from status_v3 import activity_summary, evolution_progress
from store_v3 import Store, dump


class StatusTests(unittest.TestCase):
    def setUp(self):
        root = Path('D:/量化数据/work')
        root.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix='status-check-', dir=root)
        self.store = Store(Path(self.tmp.name) / 'status.sqlite3')
        self.clock = datetime(2026, 10, 13, 10, tzinfo=TZ)
        self.clock_patch = patch('status_v3.datetime')
        self.mock_clock = self.clock_patch.start()
        self.mock_clock.now.side_effect = lambda tz: self.clock
        self.store.set('work', dict(active=True, status='running', message='观察中'))
        self.store.set('terminated_source_complete', True)
        self.store.set('daily_date', '2026-10-12')
        with self.store.db() as db:
            db.execute('INSERT INTO securities VALUES(?,?,?,?,?,?,?)', ('sh.600000', '测试', '', '', 1, 'mainboard', '{}'))
            db.execute('INSERT INTO prepared VALUES(?,?,?,?,?)', ('sh.600000', '2026-10-12', '2023-10-13', '', '2026-10-12'))
            db.execute('INSERT INTO calendar VALUES(?,?)', ('2026-10-13', 1))

    def tearDown(self):
        self.clock_patch.stop()
        self.tmp.cleanup()

    def plan(self, items=None, day='2026-10-13', market=None):
        self.store.plan_save('paper-steady', 'steady', day, dict(items=items or [], screened=3197,
                             asof='2026-10-12', market=market or {}, excluded={'未符合策略规则': 3197}))

    def challenger(self, days=None, control_days=None, closed=0):
        cid = 'candidate-check'
        with self.store.db() as db:
            db.execute('INSERT INTO strategies VALUES(?,?,?,?)', (cid, 'steady', 'challenger', dump(dict(created='2026-10-13T09:00:00+08:00'))))
        shadow = self.store.empty_account(22000)
        shadow.update(days=days or [], closed=closed)
        control = self.store.empty_account(22000)
        control['days'] = control_days if control_days is not None else shadow['days']
        self.store.save_account('shadow-' + cid, shadow)
        self.store.save_account('control-' + cid, control)
        return cid

    def test_no_opportunity_uses_recorded_market_reason_and_analysis_time(self):
        self.plan(market=dict(allow_new=False, reason='沪深300趋势偏弱，稳健模式暂停新增买入'))
        result = activity_summary(self.store, 'steady', 'trading')
        self.assertEqual(result['status'], 'no_opportunity')
        self.assertIn('趋势偏弱', result['reason'])
        self.assertEqual(result['last_analysis_at'], self.store.latest_plan('paper-steady')['created'])
        self.assertEqual(result['screened'], 3197)

    def test_work_error_has_priority_then_pause_then_incomplete_data(self):
        self.store.set('work', dict(active=False, status='error', error='查询失败'))
        self.assertEqual(activity_summary(self.store, 'steady')['reason'], '查询失败')
        self.store.set('work', dict(active=False, status='paused'))
        self.assertEqual(activity_summary(self.store, 'steady')['status'], 'paused')
        self.store.set('work', dict(active=True, status='running'))
        self.store.set('daily_date', '2026-10-13')
        self.assertEqual(activity_summary(self.store, 'steady')['status'], 'preparing')

    def test_trading_stale_quote_pauses_but_closed_quote_does_not_report_error(self):
        self.plan(items=[dict(code='sh.600000', name='测试')])
        self.store.set('quote_health', dict(status='paused', fetched='2026-10-13T10:00:00+08:00'))
        self.assertEqual(activity_summary(self.store, 'steady', 'trading')['status'], 'quote_paused')
        self.assertEqual(activity_summary(self.store, 'steady', 'closed')['status'], 'waiting')

    def test_unrelated_missing_real_quote_does_not_pause_empty_mode(self):
        self.plan()
        self.store.set('quote_health', dict(status='paused', requested=4, fresh=3))
        self.assertEqual(activity_summary(self.store, 'steady', 'trading')['status'], 'no_opportunity')

    def test_source_timestamp_not_fetch_timestamp_proves_freshness(self):
        self.plan(items=[dict(code='sh.600000')])
        q = dict(source_time='2026-10-13T09:55:00', fetched='2026-10-13T10:00:00', price=10, volume=100)
        with self.store.db() as db:
            db.execute('INSERT INTO quotes VALUES(?,?)', ('sh.600000', dump(q)))
        self.store.set('quote_health', dict(fetched=q['fetched'], status='healthy'))
        result = activity_summary(self.store, 'steady', 'trading')
        self.assertEqual(result['status'], 'quote_paused')
        self.assertEqual(result['quote_source_at'], q['source_time'])

    def test_expired_and_future_plan_are_not_today_monitoring(self):
        self.plan(items=[dict(code='sh.600000')], day='2026-10-12')
        self.assertEqual(activity_summary(self.store, 'steady', 'closed')['status'], 'analyzing')
        self.plan(items=[dict(code='sh.600000')], day='2026-10-14')
        self.assertEqual(activity_summary(self.store, 'steady', 'closed')['label'], '下一交易日计划已准备')

    def test_ordinary_paper_success_does_not_count_as_candidate_observation(self):
        account = self.store.account('paper-steady')
        account.update(days=[str(i) for i in range(40)], closed=30)
        self.store.save_account('paper-steady', account)
        result = evolution_progress(self.store)['steady']
        self.assertIsNone(result['candidate_id'])
        self.assertEqual((result['days'], result['trades']), (0, 0))
        self.assertIn('尚未开始', result['reason'])

    def test_common_observation_days_only_and_candidate_closed_only(self):
        cid = self.challenger(['2026-10-09', '2026-10-12', '2026-10-13', '2026-10-13'], ['2026-10-12', '2026-10-13'], 3)
        account = self.store.account('paper-steady')
        account.update(days=[str(i) for i in range(40)], closed=30)
        self.store.save_account('paper-steady', account)
        result = evolution_progress(self.store)['steady']
        self.assertEqual(result['candidate_id'], cid)
        self.assertEqual((result['days'], result['trades']), (2, 3))
        self.assertEqual((result['remaining_days'], result['remaining_trades']), (18, 7))

    def test_candidate_threshold_is_not_automatic_pass(self):
        days = [f'2026-09-{i:02d}' for i in range(1, 26)]
        self.challenger(days, days, 11)
        result = evolution_progress(self.store)['steady']
        self.assertEqual(result['stage'], 'forward')
        self.assertIn('仍需', result['reason'])
        self.assertEqual((result['remaining_days'], result['remaining_trades']), (0, 0))

    def test_incomplete_delisting_roster_explains_no_observation(self):
        self.store.set('terminated_source_complete', False)
        self.store.set('research_wait', '退市名录尚未核实完整，暂停升级验证')
        result = evolution_progress(self.store)['steady']
        self.assertEqual(result['stage'], 'blocked')
        self.assertIn('退市名录', result['reason'])
        self.assertIsNone(result['candidate_id'])
        self.assertEqual(result['days'], 0)

    def test_pause_keeps_candidate_progress_and_does_not_claim_running(self):
        self.challenger(['2026-10-13'], closed=1)
        self.store.set('work', dict(active=False, status='paused'))
        result = evolution_progress(self.store)['steady']
        self.assertEqual(result['stage'], 'paused')
        self.assertEqual(result['days'], 1)
        self.assertIn('开始工作', result['reason'])

    def test_missing_candidate_accounts_are_visible_and_do_not_raise(self):
        cid = self.challenger()
        with self.store.db() as db:
            db.execute('DELETE FROM accounts WHERE id=?', ('control-' + cid,))
        self.assertEqual(evolution_progress(self.store)['steady']['stage'], 'blocked')

    def test_helpers_never_write_or_change_ledger(self):
        self.plan()
        before = copy.deepcopy(self.store.page('paper'))
        with patch.object(self.store, 'set', side_effect=AssertionError('read-only')):
            activity_summary(self.store, 'steady', 'trading')
            evolution_progress(self.store)
        self.assertEqual(before, self.store.page('paper'))

    def test_seed_with_null_evidence_is_an_unobserved_candidate(self):
        current = self.store.strategy('steady')
        payload = current['payload'] | dict(evidence=None, forward_evidence=None)
        with self.store.db() as db:
            db.execute('UPDATE strategies SET payload=? WHERE id=?', (dump(payload), current['id']))
        result = evolution_progress(self.store)['steady']
        self.assertEqual((result['days'], result['trades']), (0, 0))
        self.assertIsNone(result['candidate_id'])

    def test_new_running_comparison_takes_priority_over_previous_failed_candidate(self):
        with self.store.db() as db:
            db.execute('INSERT INTO experiments VALUES(?,?,?,?)', ('old-failure', 'steady', '2026-10-01T10:00:00+08:00', dump(dict(gate=dict(passed=False, reasons=['费用条件未通过'])))))
        self.store.set('research_wait', '比较策略中；最多同时4个计算任务')
        self.store.set('research-progress-steady', dict(progress=30, message='稳健策略比较 20/60'))
        result = evolution_progress(self.store)['steady']
        self.assertEqual(result['stage'], 'historical')
        self.assertEqual(result['reason'], '稳健策略比较 20/60')
        self.assertEqual(result['days'], 0)

    def test_paper_page_can_explicitly_select_paper_account_during_real_stage(self):
        self.plan()
        a = self.store.account('real')
        a['positions']['sh.600000'] = dict(code='sh.600000', name='测试持仓', quantity=100, available=100, avg=10., book_cost=1000., date='2026-10-12', kind='stock', lots=[dict(date='2026-10-12', quantity=100)], stop=9., target=12., peak=10., hold=25)
        self.store.save_account('real', a)
        self.store.set('settings', self.store.get('settings') | dict(stage='real'))
        self.assertEqual(activity_summary(self.store, 'steady', 'trading')['status'], 'quote_paused')
        result = activity_summary(self.store, 'steady', 'trading', account_id='paper-steady')
        self.assertEqual(result['status'], 'no_opportunity')

    def test_paused_account_still_explains_last_recorded_selection(self):
        self.plan(market=dict(allow_new=False, reason='沪深300趋势偏弱，稳健模式暂停新增买入'))
        self.store.set('work', dict(active=False, status='paused'))
        result = activity_summary(self.store, 'steady', 'trading')
        self.assertEqual(result['status'], 'paused')
        self.assertIn('开始工作', result['reason'])
        self.assertIn('趋势偏弱', result['last_result_reason'])

    def test_updated_history_does_not_present_old_plan_as_current_no_opportunity(self):
        self.plan()
        self.store.set('daily_date', '2026-10-13')
        with self.store.db() as db:
            db.execute('UPDATE prepared SET until=?', ('2026-10-13',))
        result = activity_summary(self.store, 'steady', 'closed')
        self.assertEqual(result['status'], 'analyzing')
        self.assertEqual(result['label'], '等待新计划')
        self.assertIn('历史行情已更新', result['reason'])


if __name__ == '__main__':
    unittest.main()
