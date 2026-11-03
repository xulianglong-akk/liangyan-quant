import copy
import sys
import tempfile
import unittest
from datetime import date,timedelta
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from engine import MODES,DEFAULT_COSTS,features,backtest,can_fill,fee,corporate_actions,plan
import app

def fixture(n=150):
    rows=[]; prev=10
    for i in range(n):
        day=(date(2026,1,1)+timedelta(days=i)).isoformat(); close=round(10+i*.04,4)
        rows.append(dict(date=day,code='sh.600036',open=prev,close=close,high=max(close,prev)*1.02,low=min(close,prev)*.98,preclose=prev,volume=10_000_000,amount=200_000_000,tradestatus='1',isST='0',pctChg=(close/prev-1)*100)); prev=close
    return rows

class EngineTests(unittest.TestCase):
    def setUp(self): self.bars=fixture();self.p=MODES['active']|{'hold':3,'target':.99,'trail':.99}
    def run_bt(self,bs=None,**kw):
        rows=bs or self.bars
        return backtest({'sh.600036':rows},{'sh.600036':'测试'},self.p,rows[60]['date'],rows[-1]['date'],costs=DEFAULT_COSTS,**kw)
    def test_next_day_only(self):
        r=self.run_bt(); self.assertTrue(r['trades']);self.assertEqual(r['trades'][0]['buy_date'],self.bars[60]['date']);self.assertGreater(r['trades'][0]['sell_date'],r['trades'][0]['buy_date'])
    def test_no_future_signal(self):
        a=copy.deepcopy(self.bars); b=copy.deepcopy(a); b[100]['close']*=1.05;b[100]['high']=b[100]['close']*1.05
        self.assertEqual(features(a[:100],self.p),features(b[:100],self.p))
        ares=self.run_bt(a); b[101]['preclose']=b[100]['close']; b[101]['open']=b[100]['close']; b[101]['high']=max(b[101]['open'],b[101]['close'])*1.02
        bres=self.run_bt(b); cutoff=a[99]['date']
        self.assertEqual([x for x in ares['curve'] if x['date']<=cutoff],[x for x in bres['curve'] if x['date']<=cutoff])
    def test_deterministic(self): self.assertEqual(self.run_bt(),self.run_bt())
    def test_plan_risk_at_top_of_buy_range(self):
        pl=plan(self.bars,self.p,100000,'test','sh.600036','测试')
        self.assertIsNotNone(pl);self.assertLessEqual(pl['quantity']*(pl['buy_high']-pl['stop']),100000*self.p['risk']);self.assertEqual(pl['quantity']%100,0)
    def test_round_lots_cash_exposure(self):
        r=self.run_bt();self.assertTrue(all(t['quantity']%100==0 for t in r['trades']));self.assertTrue(all(x['cash']>=0 for x in r['curve']));self.assertTrue(all((x['equity']-x['cash'])/x['equity']<=self.p['exposure']+.01 for x in r['curve']))
    def test_limits_suspension_st(self):
        b=self.bars[70]|{'open':11,'high':11,'low':11,'close':11,'preclose':10}
        self.assertFalse(can_fill(b,'buy',0));b.update(open=9,high=9,low=9,close=9);self.assertFalse(can_fill(b,'sell',0))
        b=self.bars[70]|{'tradestatus':'0'};self.assertFalse(can_fill(b,'buy',0));self.assertFalse(can_fill(self.bars[70]|{'isST':'1'},'buy',0))
    def test_stamp_tax_date_and_minimum(self):
        self.assertAlmostEqual(fee(10000,'sell','2023-08-28',DEFAULT_COSTS),10.1);self.assertAlmostEqual(fee(10000,'sell','2023-08-27',DEFAULT_COSTS),15.1);self.assertAlmostEqual(fee(10000,'buy','2026-01-01',DEFAULT_COSTS),5.1)
    def test_action_reject_and_causal_adjustment(self):
        b=copy.deepcopy(self.bars)
        for row in b[70:]:
            for k in ['open','high','low','close','preclose']: row[k]*=.9
        self.assertIn(b[70]['date'],corporate_actions(b))
        with self.assertRaisesRegex(ValueError,'公司行动'): self.run_bt(b)
        f=features(b[:72],self.p); original=features(self.bars[:72],self.p); self.assertAlmostEqual(f['momentum'],original['momentum'],places=6)
    def test_cancel(self):
        with self.assertRaises(InterruptedError): self.run_bt(cancel=lambda:True)
    def test_inadequate_data(self):
        with self.assertRaises(ValueError): backtest({}, {}, self.p,'2026-01-01','2026-01-20')
    def test_holdout_plan_inputs_unchanged(self):
        bs=copy.deepcopy(self.bars); p=copy.deepcopy(self.p);self.run_bt(bs);self.assertEqual(bs,self.bars);self.assertEqual(p,self.p)

class LedgerAndVersionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=app.ROOT/'work');self.old=app.DB; app.DB=Path(self.temp.name)/'test.sqlite3';app.init();app.put('settings',app.get('settings')|{'auto_update':False});app.JOB['status']='idle'
        with app.connect() as db:
            for d in ['2026-09-28','2026-09-29','2026-09-30']:db.execute('INSERT INTO calendar VALUES(?,1)',(d,))
    def tearDown(self): app.DB=self.old;self.temp.cleanup()
    def trade(self,**kw):return app.add_trade(dict(account='sim',code='sh.600036',side='buy',date='2026-09-28',price=10,quantity=100,fees=5)|kw)
    def test_t_plus_one_no_short(self):
        self.trade()
        with self.assertRaisesRegex(ValueError,r'T\+1'):self.trade(side='sell')
        self.trade(side='sell',date='2026-09-29')
        with self.assertRaises(ValueError):self.trade(side='sell',date='2026-09-30')
    def test_real_capital_required(self):
        with self.assertRaisesRegex(ValueError,'真实资金'):self.trade(account='real')
    def test_lots_and_fraction(self):
        with self.assertRaises(ValueError):self.trade(quantity=101)
        with self.assertRaises(ValueError):self.trade(quantity=100.5)
    def test_cash_limit_calendar(self):
        with self.assertRaisesRegex(ValueError,'资金不足'):self.trade(price=2000)
        with self.assertRaisesRegex(ValueError,'交易日历'):self.trade(date='2026-09-27')
    def test_failed_candidate_keeps_current(self):
        with app.connect() as db:db.execute('INSERT INTO versions VALUES(?,?,?,?,?,?)',('bad',app.now(),'steady','{}','candidate','{"passed":false}'))
        with self.assertRaisesRegex(ValueError,'未通过'):app.promote('bad')
        self.assertEqual(app.params('steady')[0],'v1-steady')
    def test_freeze_and_rollback(self):
        app.put('settings',app.get('settings')|{'frozen':True})
        with self.assertRaisesRegex(ValueError,'冻结'):app.promote('v1-steady',True)
    def test_positive_promotion_rollback(self):
        # Isolate recommendation data; only test the version transaction here.
        original=app.make_recommendations;app.make_recommendations=lambda:True
        try:
            with app.connect() as db:db.execute('INSERT INTO versions VALUES(?,?,?,?,?,?)',('good',app.now(),'steady',__import__('json').dumps(MODES['steady']),'candidate','{"passed":true}'))
            app.promote('good');self.assertEqual(app.params('steady')[0],'good');app.promote('v1-steady',True);self.assertEqual(app.params('steady')[0],'v1-steady')
        finally:app.make_recommendations=original
    def test_job_completion_is_atomic(self):
        import time
        from unittest.mock import patch
        with patch.object(app,'make_recommendations',return_value=True):
            for i in range(5):
                app.start_job('plans');deadline=time.time()+2
                while app.JOB['status']=='running' and time.time()<deadline:time.sleep(.005)
                self.assertEqual(app.JOB['status'],'done');self.assertIsNotNone(app.JOB['ended']);self.assertIsNotNone(app.JOB['seconds'])

if __name__=='__main__':unittest.main(verbosity=2)
