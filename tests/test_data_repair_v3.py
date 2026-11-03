import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from runtime_v3 import Runtime
from rules_v3 import TZ
from store_v3 import Store,dump

DAYS=['2026-10-19','2026-10-20','2026-10-21','2026-10-22','2026-10-23']
CODES=['sz.%06d'%i for i in range(1100)]

def bar(code,day,close=10.,preclose=None):
    return dict(code=code,date=day,open=close,high=close,low=close,close=close,
                preclose=close if preclose is None else preclose,volume=1e6,amount=1e7,
                tradestatus='1',isST='0',pctChg=0)

def daily_rows(day,close=10.,preclose=None):
    return [bar(c,day,close,preclose) for c in CODES]

class DailySource:
    def __init__(self,days=None):
        self.calls=[]; self.days=days or {}
    def call(self,kind,args,cancel=lambda:False):
        if kind!='daily': raise AssertionError('不应重新获取历史或名录')
        self.calls.append((kind,args['day']))
        raw=self.days.get(args['day'])
        if raw is None: raise AssertionError('未准备该日行情：'+args['day'])
        return raw

class WindowSource:
    def __init__(self): self.args=None
    def call(self,kind,args,cancel=lambda:False):
        self.args=args; raise RuntimeError('模拟停止，只检查请求区间')

class DataRepairTests(unittest.TestCase):
    def setUp(self):
        folder=Path('D:/量化数据/work'); folder.mkdir(parents=True,exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(prefix='repair-check-',dir=folder); self.root=Path(self.tmp.name)
        (self.root/'work').mkdir(); (self.root/'outputs').mkdir()
        self.store=Store(self.root/'test.sqlite3')
        with self.store.db() as db:
            for day in DAYS: db.execute('INSERT OR REPLACE INTO calendar VALUES(?,1)',(day,))
            for c in CODES: db.execute('INSERT INTO securities VALUES(?,?,?,?,?,?,?)',(c,c,'2000-01-01','',1,'mainboard','{}'))
        self.store.set('history_start',DAYS[0]); self.store.set('bootstrap_date',DAYS[-1])
        self.source=DailySource(); self.runtime=Runtime(self.store,self.root,source=self.source,
                                                       clock=lambda:datetime(2026,10,23,20,tzinfo=TZ))
        self.runtime.cancel.clear()
    def tearDown(self):
        self.runtime.stop(); self.runtime.shutdown.set()
        for thread in (self.runtime.mon_thread,self.runtime.data_thread):
            if thread and thread.is_alive(): thread.join(3)
        try: self.tmp.cleanup()
        except PermissionError: time.sleep(.3); self.tmp.cleanup()
    def seed_bars(self,days,close=10.):
        with self.store.db() as db:
            for day in days:
                for c in CODES: db.execute('INSERT OR REPLACE INTO bars VALUES(?,?,?,?)',(c,day,dump(bar(c,day,close)),'seed'))
    def seed_prepared(self,until,error=''):
        with self.store.db() as db:
            for c in CODES: db.execute('INSERT OR REPLACE INTO prepared VALUES(?,?,?,?,?)',(c,until,DAYS[0],error,until))
    def errors(self):
        with self.store.db() as db: return db.execute("SELECT count(*) FROM prepared WHERE error!=''").fetchone()[0]
    def test_cross_gap_preclose_is_not_a_corporate_action(self):
        # 10-22 缺失时，10-23 的 preclose 与 10-19 收盘不同只是几天价格变化。
        self.seed_bars([DAYS[0]]); self.seed_prepared(DAYS[0])
        self.store.set('daily_date',DAYS[0]); self.store.set('daily_count',len(CODES))
        self.runtime.commit_daily(daily_rows(DAYS[-1],12.,12.),DAYS[-1])
        self.assertEqual(self.errors(),0)
        self.assertEqual(self.store.get('daily_date'),DAYS[-1])
        with self.store.db() as db:
            values={r[0] for r in db.execute('SELECT DISTINCT until FROM prepared')}
        self.assertEqual(values,{DAYS[0]})
    def test_adjacent_preclose_mismatch_still_flags_action(self):
        self.seed_bars([DAYS[2]]); self.seed_prepared(DAYS[2])
        self.store.set('daily_date',DAYS[2]); self.store.set('daily_count',len(CODES))
        self.runtime.commit_daily(daily_rows(DAYS[3],9.5,9.5),DAYS[3])
        with self.store.db() as db:
            flagged=db.execute("SELECT count(*) FROM prepared WHERE error='新增公司行动待核实'").fetchone()[0]
        self.assertEqual(flagged,len(CODES))
    def test_gap_repair_clears_false_flag_and_completes_coverage(self):
        # 10-22 整天缺失，prepared 停在 10-21 并带误报；修复后无需逐只重下历史。
        self.seed_bars([DAYS[0],DAYS[1],DAYS[2],DAYS[4]]); self.seed_prepared(DAYS[2],'新增公司行动待核实')
        self.store.set('daily_date',DAYS[4]); self.store.set('daily_count',len(CODES))
        self.source.days[DAYS[3]]=daily_rows(DAYS[3])
        self.assertEqual(self.runtime.clear_gap_flags(),len(CODES))
        self.assertEqual(self.errors(),0)
        self.assertEqual(self.runtime.repair_daily_gaps(DAYS[4]),1)
        self.assertEqual(self.source.calls,[('daily',DAYS[3])])
        self.assertEqual(self.runtime.reconcile_prepared(DAYS[4]),len(CODES))
        coverage=self.store.coverage()
        self.assertTrue(coverage['complete'])
        self.assertEqual(coverage['prepared'],len(CODES))
        self.assertEqual(coverage['asof'],DAYS[4])
        self.assertEqual(self.errors(),0)
    def test_delisted_incremental_window_never_starts_after_it_ends(self):
        # 退市股上次记为 10-21，但实际行情终点是 2025-04-30；增量起点必须回退。
        self.store.set('history_start','2023-11-01')
        with self.store.db() as db:
            db.execute("UPDATE securities SET delisted='2025-04-30',active=0 WHERE code=?",(CODES[0],))
            db.execute('INSERT OR REPLACE INTO prepared VALUES(?,?,?,?,?)',(CODES[0],'2026-10-21','2023-11-01','','2026-10-21'))
            metadata=dict(db.execute('SELECT * FROM securities WHERE code=?',(CODES[0],)).fetchone())
        source=WindowSource(); self.runtime.source=source
        with self.assertRaises(RuntimeError): self.runtime.prepare_one(metadata,'2026-10-23')
        self.assertLessEqual(source.args['start'],source.args['end'])
        self.assertEqual(source.args['end'],'2025-04-30')
    def test_start_resumes_live_worker_instead_of_failing(self):
        self.runtime.quote_source=lambda codes:{}
        release=threading.Event(); worker=threading.Thread(target=release.wait); worker.start()
        self.runtime.data_thread=worker
        result=self.runtime.start()
        self.assertTrue(result['ok']); self.assertEqual(result.get('message'),'继续上次任务')
        self.assertFalse(self.runtime.cancel.is_set())
        self.assertTrue(self.store.get('work')['active'])
        release.set(); worker.join(2); self.runtime.stop()
    def test_pending_skips_prepared_delisted_samples_on_new_day(self):
        end=DAYS[-1]; self.seed_prepared(end)
        with self.store.db() as db:
            db.execute('INSERT INTO securities VALUES(?,?,?,?,?,?,?)',('sh.600999','退市样本','2000-01-01','2026-10-22',0,'mainboard','{}'))
            db.execute('INSERT OR REPLACE INTO prepared VALUES(?,?,?,?,?)',('sh.600999',end,DAYS[0],'',end))
        self.assertEqual(self.runtime.pending_securities(end),[])
        with self.store.db() as db: db.execute("UPDATE prepared SET until='2026-10-21' WHERE code='sh.600999'")
        self.assertEqual([r['code'] for r in self.runtime.pending_securities(end)],['sh.600999'])
        with self.store.db() as db:
            db.execute('UPDATE prepared SET until=? WHERE code=?',(end,'sh.600999'))
            db.execute('INSERT INTO securities VALUES(?,?,?,?,?,?,?)',('sh.601000','活跃样本','2000-01-01','',1,'mainboard','{}'))
            db.execute('INSERT OR REPLACE INTO prepared VALUES(?,?,?,?,?)',('sh.601000','2026-10-21',DAYS[0],'','2026-10-21'))
        self.assertEqual([r['code'] for r in self.runtime.pending_securities(end)],['sh.601000'])
    def test_start_with_complete_data_does_not_refetch_history(self):
        self.seed_bars(DAYS); self.seed_prepared(DAYS[-1])
        self.store.set('daily_date',DAYS[-1]); self.store.set('daily_count',len(CODES))
        self.store.set('terminated_source_complete',False)
        self.store.set('work',self.store.get('work')|dict(active=True,status='running'))
        self.runtime.cancel.clear()
        thread=threading.Thread(target=self.runtime.data_loop); thread.start()
        end=time.monotonic()+5
        while time.monotonic()<end and self.store.get('work',{}).get('progress')!=100: time.sleep(.02)
        self.runtime.stop(); thread.join(3)
        self.assertEqual(self.source.calls,[])
        self.assertEqual(self.store.get('work')['status'],'paused')

if __name__=='__main__': unittest.main(verbosity=2)
