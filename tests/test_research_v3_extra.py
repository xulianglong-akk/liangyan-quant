"""Timing and evidence regressions for the historical/paper research gates."""
import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from research_v3 import replay,forward_gate,historical_gate,candidate_grid
from rules_v3 import PARAMS,LIMITS,COSTS,action_adjust

DAYS=['2026-10-09','2026-10-12','2026-10-13']

def bars(code):
    return [dict(code=code,date=d,open=10.,high=10.2,low=9.95,close=10.,preclose=10.,volume=20000000.,amount=200000000.,tradestatus='1',isST='0') for d in DAYS]

def ready(day):
    return {day:dict(close=10.,atr=.15,score=1.,volume=20000000.)}

def account(**extra):
    return dict(capital=22000.,cash=22000.,positions={},receivables=[],days=DAYS,closed=0,peak=22000.,halt=False,invalid=[])|extra

class ResearchEvidenceTests(unittest.TestCase):
    def simulate(self,dataset,signals,meta=None,actions=None,qty=None):
        metadata=meta or {c:dict(ipo='2000-01-01',delisted='') for c in dataset}
        def compiled(bs,p): return signals.get(bs[0]['code'],{})
        with patch('research_v3.compiled_signals',side_effect=compiled),patch('research_v3.quantity',side_effect=qty or (lambda *a:100)):
            # These injected three-day fixtures isolate execution timing of the
            # legacy branch; the quality seed needs 61 completed benchmark bars.
            return replay(dataset,metadata,actions or {},PARAMS['steady']|dict(quality=False),'steady',DAYS[0],DAYS[-1],benchmark=next(iter(dataset.values())))

    def test_opening_size_does_not_see_later_close(self):
        a,b='sh.600000','sh.600001'; ds={a:bars(a),b:bars(b)}
        ds[a][-1]['high']=12.; ds[a][-1]['close']=11.9
        signals={a:ready(DAYS[0]),b:ready(DAYS[1])}; first=[]; second=[]
        signals[a][DAYS[0]]['atr']=.5
        self.simulate(ds,signals,qty=lambda *args:(first.append(args) or 100))
        ds[a][-1]['close']=10.
        self.simulate(ds,signals,qty=lambda *args:(second.append(args) or 100))
        self.assertEqual(first,second)
        self.assertEqual(len(first),2)

    def test_intraday_exit_cannot_free_an_earlier_opening_slot(self):
        codes=['sh.600000','sh.600001','sh.600002','sh.600003']; ds={c:bars(c) for c in codes}
        ds[codes[0]][-1].update(low=8.,close=9.,high=10.2)
        signals={c:ready(DAYS[0]) for c in codes[:3]}; signals[codes[3]]=ready(DAYS[1])
        result=self.simulate(ds,signals)
        self.assertTrue(any(f['side']=='sell' for f in result['fills']))
        self.assertFalse(any(f['side']=='buy' and f['code']==codes[3] for f in result['fills']))

    def test_unsettled_bonus_cannot_support_promotion(self):
        c='sh.600000'; event=dict(date=DAYS[-1],cash=0,bonus=.1,pay_date='',listing_date='2026-10-14',supported=True)
        result=self.simulate({c:bars(c)},{c:ready(DAYS[0])},actions={c:{DAYS[-1]:event}})
        self.assertTrue(any('未完成权益' in reason for reason in result['invalid']))

    def test_delisting_never_disappears_into_a_clean_profit(self):
        c='sh.600000'; result=self.simulate({c:bars(c)},{c:ready(DAYS[0])},meta={c:dict(ipo='2000-01-01',delisted=DAYS[-1])})
        self.assertTrue(any('退市' in reason for reason in result['invalid']))

    def test_incomplete_rights_record_cannot_explain_a_price_reset(self):
        c='sh.600000'; ds={c:bars(c)}; ds[c][-1].update(preclose=8.,open=8.,high=8.2,low=7.95,close=8.)
        event=dict(date=DAYS[-1],cash=0,bonus=0,pay_date='',listing_date='',supported=True)
        result=self.simulate(ds,{c:ready(DAYS[0])},actions={c:{DAYS[-1]:event}})
        self.assertTrue(any('不能解释除权参考价' in reason for reason in result['invalid']))

    def test_cash_dividend_tax_is_a_conservative_warning_not_a_blocker(self):
        c='sh.600000'; ds={c:bars(c)}
        ds[c][-1].update(preclose=9.9,open=9.9,high=10.2,low=9.85,close=10.)
        event=dict(date=DAYS[-1],cash=.1,bonus=0,pay_date=DAYS[-1],listing_date='',supported=True)
        result=self.simulate(ds,{c:ready(DAYS[0])},actions={c:{DAYS[-1]:event}})
        self.assertEqual(result['invalid'],[])
        self.assertTrue(any('现金分红' in w for w in result['warnings']))
    def test_fractional_bonus_is_floored_and_flagged(self):
        h=dict(code='sh.600000',quantity=100,stop=9.,target=12.,peak=11.,distance=1.,price=10.)
        event=dict(supported=True,cash=0.,bonus=1/3,pay_date='2026-10-14',listing_date='2026-10-15')
        r=action_adjust(h,event)
        self.assertEqual(r['quantity'],33); self.assertTrue(r['fractional'])
    def test_slippage_and_fees_are_charged(self):
        c='sh.600000'; result=self.simulate({c:bars(c)},{c:ready(DAYS[0])})
        fill=result['fills'][0]
        self.assertGreater(fill['price'],10.)
        self.assertGreater(fill['fees'],0.)
        self.assertLess(result['metrics']['total_return'],0.)

    def test_training_grid_is_exactly_sixty_unique_rules(self):
        for mode in PARAMS:
            grid=candidate_grid(mode); self.assertEqual(len(grid),60)
            self.assertEqual(len({tuple(sorted(p.items())) for p in grid}),60)
            self.assertTrue(all(p['hold']>0 for p in grid))

    def test_forward_comparison_needs_common_observation_days(self):
        dates=[f'2026-09-{i:02}' for i in range(1,21)]
        candidate=account(cash=23000.,peak=23000.,days=dates,closed=10)
        control=account(days=[])
        self.assertEqual(forward_gate(candidate,control,LIMITS['steady']),'waiting')
        control['days']=dates
        self.assertEqual(forward_gate(candidate,control,LIMITS['steady']),'passed')
        control['invalid']=['行情缺失']
        self.assertEqual(forward_gate(candidate,control,LIMITS['steady']),'failed')

    def test_duplicate_observation_days_and_invalid_candidate_do_not_pass(self):
        candidate=account(cash=23000.,peak=23000.,days=['same']*20,closed=10)
        self.assertEqual(forward_gate(candidate,account(days=['same']*20),LIMITS['steady']),'waiting')
        candidate['invalid']=['未完成权益']
        self.assertEqual(forward_gate(candidate,account(),LIMITS['steady']),'failed')

    def test_nonfinite_metrics_do_not_pass_the_historical_gate(self):
        good=dict(metrics=dict(total_return=.1,max_drawdown=.01,closed=10,benchmark_return=.05),invalid=[])
        bad=copy.deepcopy(good); bad['metrics']['total_return']=float('nan')
        self.assertFalse(historical_gate(bad,good,good,'steady')['passed'])

    def test_forward_waits_for_pending_bonus_or_cash_settlement(self):
        dates=[f'2026-09-{i:02}' for i in range(1,21)]
        candidate=account(cash=23000.,peak=23000.,days=dates,closed=10,receivables=[dict(cash=0,quantity=10,price=10.)])
        self.assertEqual(forward_gate(candidate,account(days=dates),LIMITS['steady']),'waiting')

if __name__=='__main__': unittest.main(verbosity=2)
