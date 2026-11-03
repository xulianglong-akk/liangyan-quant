import sys
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import app
from test_engine import fixture

class Result:
    def __init__(self,rows=None,error='0'):
        self.rows=rows or [];self.fields=list(self.rows[0]) if self.rows else [];self.error_code=error;self.error_msg='mock failure' if error!='0' else 'success';self.i=-1
    def next(self):self.i+=1;return self.i<len(self.rows)
    def get_row_data(self):return [str(self.rows[self.i][k]) for k in self.fields]

class DataTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=app.ROOT/'work');self.old=app.DB;app.DB=Path(self.tmp.name)/'data.sqlite3';app.init();app.put('pool',{'sh.600036':'测试'});app.CANCEL.clear();self.rows=fixture(150)
        self.fake=SimpleNamespace(login=lambda:Result(),logout=lambda:None,query_trade_dates=lambda **kw:Result([{'calendar_date':b['date'],'is_trading_day':'1'} for b in self.rows]),query_stock_basic=lambda:Result([dict(code='sh.600036',code_name='测试',status='1',ipoDate='2020-01-01')]),query_history_k_data_plus=lambda *a,**kw:Result(self.rows))
    def tearDown(self):app.CANCEL.clear();app.DB=self.old;self.tmp.cleanup()
    def update(self):
        with patch.dict(sys.modules,{'baostock':self.fake}):app.update_data()
    def test_incremental_repeat_no_duplicates(self):
        self.update();self.update();self.assertEqual(len(app.load_bars('sh.600036')),150)
        with app.connect() as db:self.assertGreater(db.execute('SELECT count(*) FROM recommendations').fetchone()[0],0)
    def test_corruption_keeps_cache(self):
        self.update();before=app.load_bars('sh.600036');broken=[dict(b) for b in self.rows];broken[-1]['high']=0
        self.fake.query_history_k_data_plus=lambda *a,**kw:Result(broken)
        with self.assertRaises(RuntimeError):self.update()
        self.assertEqual(before,app.load_bars('sh.600036'))
    def test_missing_internal_bar_rejected(self):
        self.fake.query_history_k_data_plus=lambda *a,**kw:Result(self.rows[:80]+self.rows[81:])
        with self.assertRaisesRegex(RuntimeError,'缺失'):self.update()
        self.assertEqual(app.load_bars('sh.600036'),[])
    def test_partial_update_resume(self):
        self.fake.query_history_k_data_plus=lambda code,*a,**kw:Result(self.rows) if code=='sh.600036' else Result(error='99')
        with self.assertRaises(RuntimeError):self.update()
        self.assertEqual(len(app.load_bars('sh.600036')),150);self.assertEqual(app.load_bars('sh.000300'),[])
        self.fake.query_history_k_data_plus=lambda *a,**kw:Result(self.rows);self.update();self.assertEqual(len(app.load_bars('sh.000300')),150)
    def test_cancel_stops_commit(self):
        app.CANCEL.set()
        with self.assertRaises(InterruptedError):self.update()
        self.assertEqual(app.load_bars('sh.600036'),[])

if __name__=='__main__':unittest.main(verbosity=2)
