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

DAY='2026-10-12'

def bar(code,date=DAY):
    return dict(code=code,date=date,open=10.,high=10.1,low=9.9,close=10.,preclose=10.,volume=1000000.,amount=10000000.,tradestatus='1',isST='0',pctChg=0)

class HistorySource:
    def __init__(self): self.calls=[]; self.block={}; self.entered={}; self.fail={}
    def call(self,kind,args,cancel):
        if kind!='history': raise AssertionError('不应重新获取已存在的初始数据')
        code=args['code']; self.calls.append(code); self.entered.setdefault(code,threading.Event()).set()
        release=self.block.get(code)
        while release and not release.wait(.01):
            if cancel(): raise InterruptedError('已取消')
        if cancel(): raise InterruptedError('已取消')
        if self.fail.get(code,0): self.fail[code]-=1; raise OSError('隔离连接失败')
        return dict(bars=[bar(code)],dividends=[])

class DataRetryTests(unittest.TestCase):
    def setUp(self):
        folder=Path('D:/量化数据/work'); folder.mkdir(parents=True,exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(prefix='retry-check-',dir=folder); self.root=Path(self.tmp.name)
        (self.root/'work').mkdir(); (self.root/'outputs').mkdir(); self.store=Store(self.root/'test.sqlite3')
        self.source=HistorySource(); self.runtime=Runtime(self.store,self.root,source=self.source,clock=lambda:datetime(2026,10,12,20,tzinfo=TZ))
        self.codes=['sh.600000','sh.600001','sh.600002']
        with self.store.db() as db:
            db.execute('INSERT INTO calendar VALUES(?,1)',(DAY,))
            for c in self.codes: db.execute('INSERT INTO securities VALUES(?,?,?,?,?,?,?)',(c,c,'2000-01-01','',1,'mainboard','{}'))
            db.execute('INSERT INTO prepared VALUES(?,?,?,?,?)',(self.codes[1],'2026-10-09',DAY,'此前失败',''))
            db.execute('INSERT INTO bars VALUES(?,?,?,?)',(self.codes[1],'2026-10-09',dump(bar(self.codes[1],'2026-10-09')),'old-cache'))
        self.store.set('bootstrap_date',DAY); self.store.set('history_start',DAY); self.store.set('daily_date',DAY)
        self.thread=None
    def tearDown(self):
        self.runtime.stop()
        for release in self.source.block.values(): release.set()
        if self.thread: self.thread.join(3)
        self.tmp.cleanup()
    def run_history(self):
        self.runtime.cancel.clear(); self.store.set('work',self.store.get('work')|dict(active=True,status='running'))
        self.thread=threading.Thread(target=self.runtime.data_loop); self.thread.start()
    def wait_for(self,test):
        end=time.monotonic()+3
        while time.monotonic()<end:
            if test(): return
            time.sleep(.01)
        self.fail('预期调度状态未出现')
    def error(self,code):
        with self.store.db() as db:
            row=db.execute('SELECT error FROM prepared WHERE code=?',(code,)).fetchone()
            return row['error'] if row else None
    def test_paused_request_is_persistent_deduplicated_and_keeps_cache_progress(self):
        self.store.set('work',self.store.get('work')|dict(progress=42))
        first=self.runtime.request_data_retry(); second=self.runtime.request_data_retry()
        self.assertEqual(first,dict(queued=1,count=1,active=False)); self.assertEqual(second,dict(queued=0,count=1,active=False))
        self.assertTrue(self.runtime.cancel.is_set()); self.assertIsNone(self.runtime.data_thread); self.assertEqual(self.store.get('work')['progress'],42)
        self.assertEqual(len(self.store.bars(self.codes[1])),1)
        restored=Runtime(Store(self.store.path),self.root,source=self.source)
        self.assertEqual(restored.store.get('data_retry_queue'),[self.codes[1]])
    def test_failure_retry_precedes_untouched_symbols_and_can_be_requeued(self):
        a,b,c=self.codes; self.source.block[a]=threading.Event(); self.source.block[c]=threading.Event(); self.source.fail[b]=1
        self.run_history(); self.wait_for(lambda:a in self.source.calls)
        self.assertEqual(self.runtime.request_data_retry()['queued'],1); self.source.block[a].set()
        self.wait_for(lambda:c in self.source.calls)
        self.assertEqual(self.source.calls[:3],[a,b,c]); self.assertEqual(self.error(b),'隔离连接失败')
        self.assertEqual(self.store.get('data_retry_queue'),[])
        self.assertEqual(self.runtime.request_data_retry()['queued'],1)
        self.assertEqual(self.runtime.request_data_retry()['queued'],0)
        self.source.block[c].set(); self.wait_for(lambda:self.error(b)=='')
        self.assertEqual(self.source.calls[:4],[a,b,c,b]); self.assertEqual(len(self.store.bars(b)),2)
    def test_processing_failure_is_not_queued_twice_and_cancel_resume_keeps_intent(self):
        a,b,c=self.codes; self.source.block[b]=threading.Event()
        self.runtime.request_data_retry(); self.run_history(); self.wait_for(lambda:b in self.source.calls)
        self.assertEqual(self.runtime.request_data_retry(),dict(queued=0,count=1,active=True))
        self.runtime.stop(); self.thread.join(2)
        self.assertFalse(self.thread.is_alive()); self.assertEqual(self.store.get('data_retry_queue'),[b]); self.assertFalse(self.runtime.data_inflight)
        # Cache commit precedes completion bookkeeping. Hold that legal phase
        # open to prove that a processing symbol remains deduplicated until done.
        committed=threading.Event(); finish=threading.Event(); prepare=self.runtime.prepare_one
        def delayed_finish(metadata,end):
            prepare(metadata,end)
            if metadata['code']==b: committed.set(); finish.wait(3)
        self.runtime.prepare_one=delayed_finish
        self.source.block[b].set(); self.run_history(); self.assertTrue(committed.wait(2))
        self.assertEqual(self.error(b),''); self.assertEqual(self.store.get('data_retry_queue'),[b])
        self.assertIn(b,self.runtime.data_inflight); self.assertEqual(self.runtime.request_data_retry(),dict(queued=0,count=1,active=True))
        finish.set(); self.wait_for(lambda:self.error(b)=='' and self.store.get('data_retry_queue')==[])
        self.assertEqual(self.store.get('data_retry_queue'),[]); self.assertEqual(self.source.calls[:2],[b,b])
    def test_no_failures_does_not_invent_work_or_change_progress(self):
        with self.store.db() as db: db.execute("UPDATE prepared SET error=''")
        self.assertEqual(self.runtime.request_data_retry(),dict(queued=0,count=0,active=False)); self.assertFalse(self.source.calls)

if __name__=='__main__': unittest.main(verbosity=2)
