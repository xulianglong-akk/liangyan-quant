import ctypes
import json
import multiprocessing as mp
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime,timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from runtime_v3 import Runtime,research_worker,ResearchProcessGuard,available_memory
from rules_v3 import PARAMS,COSTS,TZ,rounded
from store_v3 import Store,dump

EVIDENCE=Path('D:/量化数据/work')

def fixture(n=400,codes=4):
    dates=[]; clock=datetime(2024,1,1)
    while len(dates)<n:
        if clock.weekday()<5: dates.append(clock.date().isoformat())
        clock+=timedelta(days=1)
    dataset={}
    for j in range(codes):
        code=f'sh.{600000+j:06}'; close=10.+j/10; bars=[]
        for i,d in enumerate(dates):
            old=close; close=rounded(close*(1.002+(j%3)*.0001 if i%50<38 else .997))
            bars.append(dict(code=code,date=d,open=old,high=rounded(max(old,close)*1.006),low=rounded(min(old,close)*.994),close=close,preclose=old,volume=20000000.,amount=200000000.,tradestatus='1',isST='0'))
        dataset[code]=bars
    meta={c:dict(code=c,ipo='2000-01-01',delisted='') for c in dataset}
    return dict(dataset=dataset,metadata=meta,actions={},benchmark=next(iter(dataset.values())),costs=COSTS,consumed=None)

def process_exists(pid):
    kernel=ctypes.WinDLL('kernel32',use_last_error=True); kernel.OpenProcess.restype=ctypes.c_void_p
    handle=kernel.OpenProcess(0x1000,False,pid)
    if not handle: return False
    exitcode=ctypes.c_ulong(); kernel.GetExitCodeProcess.argtypes=[ctypes.c_void_p,ctypes.c_void_p]; kernel.CloseHandle.argtypes=[ctypes.c_void_p]
    kernel.GetExitCodeProcess(handle,ctypes.byref(exitcode)); kernel.CloseHandle(handle)
    return exitcode.value==259

@unittest.skipUnless(os.name=='nt','Windows process-tree ownership test')
class RuntimeParallelTests(unittest.TestCase):
    def setUp(self):
        EVIDENCE.mkdir(parents=True,exist_ok=True); self.tmp=tempfile.TemporaryDirectory(dir=EVIDENCE); self.root=Path(self.tmp.name)
        (self.root/'outputs').mkdir(); (self.root/'work').mkdir(); self.store=Store(self.root/'test.sqlite3'); self.runtime=Runtime(self.store,self.root)
        self.ctx=mp.get_context('spawn'); self.jobs=[]
    def tearDown(self):
        self.runtime.stop()
        for job in self.jobs:
            if not job.get('closed'): self.runtime.end_research_job(job); job['closed']=True
        try: self.tmp.cleanup()
        except PermissionError: time.sleep(.3); self.tmp.cleanup()
    def launch(self,mode,snapshot,width,cp):
        parent,child=self.ctx.Pipe(False); event=self.ctx.Event(); gate=self.ctx.Event()
        p=self.ctx.Process(target=research_worker,args=(child,str(snapshot),mode,PARAMS[mode],str(cp),event,width,gate),daemon=False); p.start()
        guard=ResearchProcessGuard(p.pid); child.close(); gate.set()
        job=dict(process=p,pipe=parent,cancel=event,guard=guard,workers=width,phase='training',closed=False); self.jobs.append(job); return job
    def finish(self,job):
        result=None; deadline=time.monotonic()+60; maximum=0; peak_memory=0
        while time.monotonic()<deadline:
            stats=job['guard'].stats(); maximum=max(maximum,max(0,len(stats['pids'])-1)); peak_memory=max(peak_memory,stats['peak_memory_mb'])
            if job['pipe'].poll(.02):
                message=job['pipe'].recv()
                if message.get('done'):
                    self.assertNotIn('error',message); result=message['result']; break
            if not job['process'].is_alive(): break
        self.assertIsNotNone(result); self.runtime.end_research_job(job); job['closed']=True; result['measured_peak_memory_mb']=peak_memory
        return result,maximum
    def test_parallel_training_matches_serial_and_checkpoint_resume(self):
        snapshot=self.root/'outputs'/'snapshot.json'; snapshot.write_text(dump(fixture()),encoding='utf-8')
        serial,one=self.finish(self.launch('active',snapshot,1,self.root/'work'/'serial.json'))
        parallel,two=self.finish(self.launch('active',snapshot,2,self.root/'work'/'parallel.json'))
        self.assertEqual(serial['trials'],parallel['trials']); self.assertEqual(serial['test'],parallel['test']); self.assertEqual(serial['params'],parallel['params'])
        self.assertEqual(len(parallel['trials']),60); self.assertGreaterEqual(two,2)
        resumed,_=self.finish(self.launch('active',snapshot,2,self.root/'work'/'parallel.json'))
        self.assertEqual(resumed['trials'],parallel['trials'])
        (EVIDENCE/'runtime-parallel-reproduction.json').write_text(dump(dict(passed=True,serial_seconds=serial['seconds'],parallel_seconds=parallel['seconds'],serial_peak_memory_mb=serial['measured_peak_memory_mb'],parallel_peak_memory_mb=parallel['measured_peak_memory_mb'],parallel_children=two,trial_count=60,checkpoint_reproduced=True)),encoding='utf-8')
    def test_four_compute_children_and_cancel_leaves_no_orphans(self):
        snapshot=self.root/'outputs'/'cancel-snapshot.json'; snapshot.write_text(dump(fixture(500,30)),encoding='utf-8')
        jobs=[self.launch(mode,snapshot,2,self.root/'work'/f'{mode}.json') for mode in PARAMS]
        deadline=time.monotonic()+30; pids=set(); observed=0
        while time.monotonic()<deadline:
            current=[job['guard'].stats()['pids'] for job in jobs]
            pids.update(pid for group in current for pid in group); observed=max(observed,sum(max(0,len(group)-1) for group in current))
            if observed>=4: break
            time.sleep(.03)
        self.assertGreaterEqual(observed,4)
        self.runtime.research_jobs={mode:job for mode,job in zip(PARAMS,jobs)}
        started=time.monotonic(); self.runtime.stop(); elapsed=time.monotonic()-started
        for job in jobs: job['closed']=True
        deadline=time.monotonic()+3
        while any(process_exists(pid) for pid in pids) and time.monotonic()<deadline: time.sleep(.03)
        remaining=[pid for pid in pids if process_exists(pid)]
        self.assertFalse(remaining)
        (EVIDENCE/'runtime-four-worker-cancel.json').write_text(dump(dict(passed=True,observed_compute_children=observed,all_research_pids=sorted(pids),remaining_pids=remaining,stop_seconds=round(elapsed,3),available_memory_mb=round(available_memory()/1048576,2))),encoding='utf-8')
    def test_completed_close_marks_do_not_create_observation_days_or_trades(self):
        day='2026-10-12'; code='sh.600000'; self.runtime.clock=lambda:datetime(2026,10,12,20,tzinfo=TZ)
        with self.store.db() as db: db.execute('INSERT INTO calendar VALUES(?,1)',(day,)); db.execute('INSERT INTO bars VALUES(?,?,?,?)',(code,day,dump(dict(code=code,date=day,close=11.)),day))
        self.store.fill('paper-steady','buy',code,10.,100,day,5.)
        self.runtime.refresh_completed_marks(day)
        a=self.store.account('paper-steady'); self.assertEqual(a['positions'][code]['price'],11.); self.assertEqual(a['days'],[])
        self.assertEqual(len(self.store.page('paper')['steady']['fills']),1); self.assertEqual(self.store.get_quotes(),{})

    def load_research_data(self,n):
        snap=fixture(n,2); days=[b['date'] for b in snap['benchmark']]; end=days[-1]
        with self.store.db() as db:
            for d in days: db.execute('INSERT OR IGNORE INTO calendar VALUES(?,1)',(d,))
            for c,bs in snap['dataset'].items():
                db.execute('INSERT OR REPLACE INTO securities VALUES(?,?,?,?,?,?,?)',(c,c,'2000-01-01','',1,'mainboard','{}'))
                db.execute('INSERT OR REPLACE INTO prepared VALUES(?,?,?,?,?)',(c,end,days[0],'',end))
                for b in bs: db.execute('INSERT OR REPLACE INTO bars VALUES(?,?,?,?)',(c,b['date'],dump(b),end))
            for b in snap['benchmark']: db.execute('INSERT OR REPLACE INTO bars VALUES(?,?,?,?)',('sh.000300',b['date'],dump(b),end))
        self.store.set('history_start',days[0]); self.store.set('daily_date',end); self.store.set('terminated_source_complete',True)
        self.runtime.clock=lambda:datetime(2026,10,12,20,tzinfo=TZ)
        return days

    def test_fixed_selection_windows_fresh_test_blocks_and_low_memory_fallback(self):
        days=self.load_research_data(400); self.runtime.cancel.clear()
        self.assertEqual(self.store.get('metrics')['configured_compute_tasks'],4); self.assertEqual(self.store.get('metrics')['configured_cpu_threads'],8)
        with patch('runtime_v3.available_memory',return_value=1536*1048576): self.runtime.research_tick()
        self.assertEqual({j['workers'] for j in self.runtime.research_jobs.values()},{1})
        self.assertEqual(self.store.get('metrics')['max_compute_tasks'],2)
        design=self.store.get('research_design-active'); original=Path(design['selection_snapshot']).read_bytes()
        self.runtime.stop()
        self.store.set('consumed-active',days[-1]); self.store.set('consumed-steady',days[-1])
        self.load_research_data(410); self.runtime.cancel.clear(); self.runtime.research_tick()
        self.assertFalse(self.runtime.research_jobs); self.assertIsNone(self.store.get('researched-active'))
        self.assertIn('20',self.store.get('research_wait'))
        self.load_research_data(420)
        with patch('runtime_v3.available_memory',return_value=1536*1048576): self.runtime.research_tick()
        self.assertEqual(len(self.runtime.research_jobs),2)
        new=json.loads(Path(self.runtime.research_jobs['active']['snapshot']).read_text(encoding='utf-8'))
        self.assertEqual(new['design']['windows'],design['windows']); self.assertEqual(new['selection_snapshot'],design['selection_snapshot'])
        self.assertEqual(Path(design['selection_snapshot']).read_bytes(),original)
        self.runtime.stop()
        (EVIDENCE/'runtime-research-design.json').write_text(dump(dict(passed=True,low_memory_tasks=2,fixed_selection_windows=design['windows'][:2],unseen_10_days_waited=True,unseen_20_days_started=True,original_selection_data_unchanged=True)),encoding='utf-8')

    def test_stop_is_not_blocked_by_large_snapshot_read_and_no_job_starts_after_stop(self):
        self.load_research_data(400); self.runtime.cancel.clear(); entered=threading.Event(); release=threading.Event(); original=self.store.bars
        def delayed(*args,**kwargs):
            entered.set(); release.wait(3); return original(*args,**kwargs)
        with patch.object(self.store,'bars',side_effect=delayed):
            reader=threading.Thread(target=self.runtime.research_tick); reader.start(); self.assertTrue(entered.wait(2))
            started=time.monotonic(); self.runtime.stop(); elapsed=time.monotonic()-started
            release.set(); reader.join(2)
        self.assertFalse(reader.is_alive()); self.assertFalse(self.runtime.research_jobs); self.assertLess(elapsed,.5)

    def test_insufficient_memory_defers_actual_research_launch(self):
        self.load_research_data(400); self.runtime.cancel.clear()
        with patch('runtime_v3.available_memory',return_value=0): self.runtime.research_tick()
        self.assertFalse(self.runtime.research_jobs); self.assertIn('内存不足',self.store.get('research_wait'))
        self.assertEqual(self.store.get('metrics')['max_compute_tasks'],2)

if __name__=='__main__': unittest.main(verbosity=2)
