import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from daily_guide import daily_decision,describe_error
from test_engine import fixture
import app

class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.settings=dict(mode='steady',sim_capital=100000,real_capital=None)
        self.stocks=[dict(code='sh.600036',last=dict(date='2026-10-09'))]
        self.rec=dict(asof='2026-10-09',entry_date='2026-10-12',mode='steady',universe=['sh.600036'],sim_capital=100000,capital=100000,capital_kind='sim',validity='待下一交易日开盘核验',stale=False,plans=[dict(quantity=100)])
        self.job=dict(status='idle')
    def decide(self,**kw):return daily_decision(kw.get('settings',self.settings),kw.get('stocks',self.stocks),kw.get('rec',self.rec),kw.get('expected','2026-10-09'),kw.get('job',self.job))
    def test_old_plan_never_ready(self):
        self.rec['validity']='有效开盘已过，仅供复盘';d=self.decide();self.assertFalse(d['ready']);self.assertEqual(d['status'],'expired');self.assertEqual(d['buy_count'],0)
    def test_stale_prices_never_ready(self):
        self.assertEqual(self.decide(expected='2026-10-12')['status'],'stale')
    def test_partial_pool_blocks(self):
        self.assertEqual(self.decide(stocks=self.stocks+[dict(code='sh.600900',last=None)])['status'],'incomplete')
    def test_changed_money_or_pool_blocks(self):
        self.assertEqual(self.decide(settings=self.settings|dict(real_capital=3000))['status'],'changed')
        self.rec['universe']=['sh.600900'];self.assertEqual(self.decide()['status'],'changed')
    def test_real_capital_used(self):
        self.settings['real_capital']=3000;self.rec.update(capital=3000,capital_kind='real')
        d=self.decide();self.assertTrue(d['ready']);self.assertEqual(d['capital'],3000);self.assertEqual(d['capital_kind'],'real')
    def test_example_amount_not_real_buy_plan(self):
        d=self.decide();self.assertFalse(d['ready']);self.assertEqual(d['status'],'needs_money');self.assertEqual(d['buy_count'],0)
    def test_affordability_not_forced(self):
        self.rec['plans'][0]['quantity']=0;self.assertEqual(self.decide()['status'],'unaffordable');self.assertFalse(self.decide()['ready'])
    def test_no_opportunity_is_honest(self):
        self.rec['plans']=[];self.assertEqual(self.decide()['status'],'no_opportunity')
    def test_missing_next_trading_day_blocks(self):
        self.rec['entry_date']=None;self.assertEqual(self.decide()['status'],'calendar_pending');self.assertFalse(self.decide()['ready'])
    def test_no_data_and_running(self):
        self.assertEqual(self.decide(stocks=[])['status'],'no_data');self.assertEqual(self.decide(job=dict(status='running',kind='update'))['status'],'updating')
    def test_error_words(self):
        self.assertIn('今天不能卖',describe_error('可卖股数不足或违反T+1')['title'])
        self.assertIn('分红或送股',describe_error('区间公司行动')['title'])
        self.assertIn('不够',describe_error('需至少250日')['title'])

class FundingPlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=app.ROOT/'work');self.old=app.DB;app.DB=Path(self.tmp.name)/'test.sqlite3';app.init();app.JOB.update(status='idle',error=None);app.put('pool',{'sh.600036':'合成测试A','sh.600900':'合成测试B'});self.bars=fixture()
        with app.connect() as db:
            for c in app.get('pool'):
                for b in self.bars:db.execute('INSERT INTO bars VALUES(?,?,?,?)',(c,b['date'],json.dumps(b|dict(code=c)),app.now()))
            for b in self.bars:db.execute('INSERT OR REPLACE INTO calendar VALUES(?,1)',(b['date'],))
    def tearDown(self):app.DB=self.old;self.tmp.cleanup()
    def latest(self,mode):
        with app.connect() as db:r=db.execute('SELECT payload FROM recommendations WHERE mode=? ORDER BY rowid DESC LIMIT 1',(mode,)).fetchone()
        return json.loads(r[0])
    def test_real_funds_replace_example(self):
        app.put('settings',app.get('settings')|dict(real_capital=500,mode='active'));app.make_recommendations();rec=self.latest('active')
        self.assertEqual(rec['capital'],500);self.assertEqual(rec['capital_kind'],'real');self.assertTrue(rec['plans']);self.assertTrue(all(p['quantity']==0 for p in rec['plans']))
    def test_combined_cost_within_cash_and_exposure(self):
        app.put('settings',app.get('settings')|dict(real_capital=30000));app.make_recommendations();rec=self.latest('active')
        self.assertLessEqual(sum(p['estimated_total'] for p in rec['plans']),min(rec['cash'],30000*app.MODES['active']['exposure']))
        for p in rec['plans']:self.assertLessEqual(p['estimated_total'],p['budget']);self.assertEqual(p['quantity']%100,0)
    def test_held_stock_not_recommended_again(self):
        app.put('settings',app.get('settings')|dict(real_capital=30000))
        with app.connect() as db:db.execute('INSERT INTO journal VALUES(?,?,?,?,?,?,?,?,?,?)',('fixture',app.now(),'real','sh.600036','buy',self.bars[-1]['date'],10,100,5,''))
        app.make_recommendations();rec=self.latest('active');self.assertNotIn('sh.600036',[p['code'] for p in rec['plans']]);self.assertEqual(rec['cash'],28995)
    def test_old_snapshots_remain(self):
        app.make_recommendations();before=self.latest('active')
        app.put('settings',app.get('settings')|dict(real_capital=500));app.make_recommendations()
        with app.connect() as db:r=db.execute('SELECT payload FROM recommendations WHERE mode=? ORDER BY rowid LIMIT 1',('active',)).fetchone()
        self.assertEqual(json.loads(r[0]),before);self.assertNotEqual(self.latest('active')['capital_kind'],before['capital_kind'])

if __name__=='__main__':unittest.main(verbosity=2)
