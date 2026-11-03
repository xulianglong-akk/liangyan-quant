import copy
import hashlib
import json
import math
import multiprocessing as mp
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime, timedelta
from pathlib import Path
from rules_v3 import TZ, PARAMS, LIMITS, COSTS, mainboard, trading_session, fresh_quote, executable, make_plan, quantity, charge, rounded, parse_action,market_context,select_diverse
from store_v3 import dump, now
from market_v3 import BaoSource, sina_quotes, validate_bars
from research_v3 import candidate_grid, replay, historical_gate, forward_gate

SESSION_NAMES=dict(trading='交易中',lunch='午间休市',auction='收盘集合竞价',closed='闭市',holiday='非交易日',unknown='交易日历准备中')

def available_memory():
    if os.name!='nt': return 0
    import ctypes
    class Memory(ctypes.Structure):
        _fields_=[('length',ctypes.c_ulong),('load',ctypes.c_ulong)]+[(k,ctypes.c_ulonglong) for k in ('total','available','page_total','page_available','virtual_total','virtual_available','extended')]
    value=Memory(); value.length=ctypes.sizeof(value)
    return value.available if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(value)) else 0

class ResearchProcessGuard:
    """Windows job owns the coordinator and every spawned child; close kills all."""
    def __init__(self,pid):
        if os.name!='nt': raise OSError('Windows进程树保护不可用')
        import ctypes
        from ctypes import wintypes
        self.ctypes=ctypes; self.handle=None; self.kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes=[ctypes.c_void_p,wintypes.LPCWSTR]; self.kernel.CreateJobObjectW.restype=wintypes.HANDLE
        self.kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]; self.kernel.OpenProcess.restype=wintypes.HANDLE
        self.kernel.AssignProcessToJobObject.argtypes=[wintypes.HANDLE,wintypes.HANDLE]; self.kernel.AssignProcessToJobObject.restype=wintypes.BOOL
        self.kernel.SetInformationJobObject.argtypes=[wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD]; self.kernel.SetInformationJobObject.restype=wintypes.BOOL
        self.kernel.QueryInformationJobObject.argtypes=[wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD,ctypes.c_void_p]; self.kernel.QueryInformationJobObject.restype=wintypes.BOOL
        self.kernel.CloseHandle.argtypes=[wintypes.HANDLE]; self.kernel.CloseHandle.restype=wintypes.BOOL
        class Basic(ctypes.Structure):
            _fields_=[('process_time',ctypes.c_longlong),('job_time',ctypes.c_longlong),('flags',wintypes.DWORD),('working_min',ctypes.c_size_t),('working_max',ctypes.c_size_t),('active_limit',wintypes.DWORD),('affinity',ctypes.c_size_t),('priority',wintypes.DWORD),('scheduling',wintypes.DWORD)]
        class Io(ctypes.Structure):
            _fields_=[(k,ctypes.c_ulonglong) for k in ('reads','writes','others','read_bytes','write_bytes','other_bytes')]
        class Extended(ctypes.Structure):
            _fields_=[('basic',Basic),('io',Io)]+[(k,ctypes.c_size_t) for k in ('process_memory','job_memory','peak_process','peak_job')]
        self.Extended=Extended
        class Pids(ctypes.Structure):
            _fields_=[('assigned',wintypes.DWORD),('count',wintypes.DWORD),('ids',ctypes.c_size_t*32)]
        self.Pids=Pids
        handle=self.kernel.CreateJobObjectW(None,None)
        if not handle: raise ctypes.WinError(ctypes.get_last_error())
        self.handle=handle; value=Extended(); value.basic.flags=0x2000
        process=self.kernel.OpenProcess(0x0100|0x0001,False,pid)
        try:
            if not process or not self.kernel.SetInformationJobObject(handle,9,ctypes.byref(value),ctypes.sizeof(value)) or not self.kernel.AssignProcessToJobObject(handle,process): raise ctypes.WinError(ctypes.get_last_error())
        except Exception: self.close(); raise
        finally:
            if process: self.kernel.CloseHandle(process)
    def stats(self):
        if not self.handle: return dict(pids=[],peak_memory_mb=0)
        c=self.ctypes; pids=self.Pids(); value=self.Extended()
        self.kernel.QueryInformationJobObject(self.handle,3,c.byref(pids),c.sizeof(pids),None)
        self.kernel.QueryInformationJobObject(self.handle,9,c.byref(value),c.sizeof(value),None)
        return dict(pids=list(pids.ids[:min(pids.count,32)]),peak_memory_mb=round(value.peak_job/1048576,2))
    def close(self):
        if self.handle: self.kernel.CloseHandle(self.handle); self.handle=None

_training_snapshot=None
_training_cancel=None
_training_cache={}

def training_initializer(snapshot_path,cancel,selection_end,costs):
    global _training_snapshot,_training_cancel,_training_cache
    _training_cancel=cancel; _training_cache={}
    if cancel.is_set(): return
    _training_snapshot=json.loads(Path(snapshot_path).read_text(encoding='utf-8'))
    _training_snapshot['costs']=costs
    _training_snapshot['dataset']={c:[b for b in bs if b['date']<=selection_end] for c,bs in _training_snapshot['dataset'].items()}
    _training_snapshot['benchmark']=[b for b in _training_snapshot['benchmark'] if b['date']<=selection_end]

def training_trial(index,params,mode,window):
    if _training_cancel.is_set(): raise InterruptedError('研究已暂停')
    s=_training_snapshot
    r=replay(s['dataset'],s['metadata'],s['actions'],params,mode,*window,s['costs'],cancel=_training_cancel.is_set,benchmark=s['benchmark'],cache=_training_cache)
    return index,dict(params=params,metrics=r['metrics'],invalid=r['invalid'])

def research_worker(pipe,snapshot_path,mode,current,checkpoint_path,cancel=None,workers=1,start_gate=None):
    """Frozen data, at most two real training processes per mode, no real ledger."""
    started=time.monotonic()
    try:
        for name in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS'): os.environ[name]='2'
        if start_gate is not None: start_gate.wait()
        check=cancel.is_set if cancel is not None else lambda:False
        if check(): raise InterruptedError('研究已暂停')
        snap=json.loads(Path(snapshot_path).read_text(encoding='utf-8')); ds=snap['dataset']; meta=snap['metadata']; actions=snap['actions']; benchmark=snap['benchmark']; costs=snap['costs']
        days=sorted({b['date'] for bs in ds.values() for b in bs})
        if len(days)<350: raise ValueError('至少需要350个交易日供按时间划分验证')
        cut1=int(len(days)*.6); cut2=int(len(days)*.8)
        design=snap.get('design'); selection_path=snap.get('selection_snapshot',snapshot_path)
        windows=[tuple(w) for w in design['windows']] if design else [(days[60],days[cut1-1]),(days[cut1],days[cut2-1]),(days[cut2],days[-1])]
        consumed=snap.get('consumed')
        if consumed:
            unseen=[d for d in days if d>consumed]
            if len(unseen)<20: raise ValueError('等待至少20个新的不重叠测试交易日')
            windows[2]=(unseen[0],unseen[-1])
        selection=snap if selection_path==snapshot_path else json.loads(Path(selection_path).read_text(encoding='utf-8'))
        selection_ds={c:[b for b in bs if b['date']<=windows[1][1]] for c,bs in selection['dataset'].items()}
        selection_bm=[b for b in selection['benchmark'] if b['date']<=windows[1][1]]
        train_cache={}; select_cache={}
        def run(p,window,cost=None): return replay(ds,meta,actions,p,mode,*window,cost or costs,cancel=check,benchmark=benchmark,cache=train_cache)
        def select_run(p,window): return replay(selection_ds,selection['metadata'],selection['actions'],p,mode,*window,costs,cancel=check,benchmark=selection_bm,cache=select_cache)
        grid=candidate_grid(mode)
        if len(grid)>60: raise ValueError('每种模式每周最多比较60组规则')
        cp=Path(checkpoint_path); raw=json.loads(cp.read_text(encoding='utf-8')) if cp.exists() else None
        if isinstance(raw,list):
            raw=dict(version=2,trials={str(i):t for i,t in enumerate(raw)},validation={},test={},windows=[list(w) for w in windows],consumed=consumed,stage='training')
        elif raw is None:
            raw=dict(version=2,trials={},validation={},test={},windows=[list(w) for w in windows],consumed=consumed,stage='training')
        elif raw.get('version')!=2: raise ValueError('断点版本不兼容；需要新的冻结快照')
        if raw.get('windows')!=[list(w) for w in windows] or raw.get('consumed')!=consumed:
            raise ValueError('断点区间已变化；需要新的冻结快照')
        state=raw; state.setdefault('trials',{}); state.setdefault('validation',{}); state.setdefault('test',{})
        records={int(i):t for i,t in state['trials'].items()}
        for i,t in records.items():
            if i>=len(grid) or t['params']!=grid[i]: raise ValueError('断点参数已变化；需要新的冻结快照')
        def persist(message=None):
            state['version']=2; state['stage']=state.get('stage','training')
            state['windows']=[list(w) for w in windows]; state['consumed']=consumed
            state['trials']={str(i):records[i] for i in sorted(records)}
            temp=cp.with_suffix('.tmp'); temp.write_text(dump(state),encoding='utf-8'); os.replace(temp,cp)
            if message: pipe.send(message)
        def save_trial(i,t):
            records[i]=t; state['stage']='training'
            persist(dict(phase='training',workers=workers,progress=round(len(records)/len(grid)*65),message=f"{LIMITS[mode]['label']}策略比较 {len(records)}/{len(grid)}"))
        pending_indices=[i for i in range(len(grid)) if i not in records]
        if workers>1 and pending_indices:
            ctx=mp.get_context('spawn')
            with ProcessPoolExecutor(max_workers=workers,mp_context=ctx,initializer=training_initializer,initargs=(selection_path,cancel,windows[1][1],costs)) as executor:
                pending={}; todo=iter(pending_indices)
                for _ in range(workers):
                    i=next(todo,None)
                    if i is not None: pending[executor.submit(training_trial,i,grid[i],mode,windows[0])]=i
                while pending:
                    if check(): raise InterruptedError('研究已暂停')
                    done,_=wait(pending,timeout=.3,return_when=FIRST_COMPLETED)
                    for future in done:
                        pending.pop(future); i,t=future.result(); save_trial(i,t)
                        i=next(todo,None)
                        if i is not None: pending[executor.submit(training_trial,i,grid[i],mode,windows[0])]=i
        else:
            for i in pending_indices:
                if check(): raise InterruptedError('研究已暂停')
                r=select_run(grid[i],windows[0]); save_trial(i,dict(params=grid[i],metrics=r['metrics'],invalid=r['invalid']))
        if len(records)!=len(grid): raise ValueError('训练断点不完整，请重新开始')
        trials=[records[i] for i in range(len(grid))]
        state['stage']='validation'; pipe.send(dict(phase='validation',workers=1,progress=70,message='训练完成，验证候选'))
        # Parameter search uses training and validation only; exactly one frozen winner sees test.
        shortlist_indices=sorted((i for i in range(len(grid)) if not records[i]['invalid']),key=lambda i:records[i]['metrics']['total_return']-records[i]['metrics']['max_drawdown'],reverse=True)[:3]
        if not shortlist_indices: raise ValueError('历史权益数据尚不足以比较策略')
        validation=state['validation']; window1=validation.setdefault('window1',{})
        comparisons=[]
        for pos,idx in enumerate(shortlist_indices):
            key=str(idx)
            if key in window1: checked=window1[key]
            else:
                pipe.send(dict(phase='validation',progress=70+round(5*(pos+1)/len(shortlist_indices)),message=f'验证候选 {pos+1}/{len(shortlist_indices)}'))
                checked=select_run(records[idx]['params'],windows[1]); window1[key]=checked; persist()
            comparisons.append((checked,records[idx]['params'],idx))
        comparisons=[c for c in comparisons if not c[0]['invalid']]
        if not comparisons: raise ValueError('验证区间权益事件尚未解决；保留当前版本')
        result,chosen,chosen_idx=max(comparisons,key=lambda t:t[0]['metrics']['total_return']-t[0]['metrics']['max_drawdown'])
        validation['chosen']=chosen_idx; persist()
        key=str(chosen_idx); window0=validation.setdefault('window0',{})
        if key not in window0:
            pipe.send(dict(phase='validation',progress=81,message='复核其他历史时间段'))
            window0[key]=select_run(chosen,windows[0]); persist()
        test=state['test']
        if test.get('chosen')!=chosen_idx: test.clear(); test['chosen']=chosen_idx
        stressed=costs|dict(commission=costs['commission']*2,minimum=costs['minimum']*2,transfer=costs['transfer']*2,slippage=costs['slippage']*2)
        pipe.send(dict(phase='test',progress=85,message='验证已冻结；开始一次独立测试',consume=windows[2][1]))
        if 'candidate' not in test:
            pipe.send(dict(phase='test',progress=86,message='独立测试：候选策略'))
            test['candidate']=run(chosen,windows[2]); persist()
        if 'baseline' not in test:
            pipe.send(dict(phase='test',progress=90,message='独立测试：当前版本'))
            test['baseline']=run(current,windows[2]); persist()
        if 'stress' not in test:
            pipe.send(dict(phase='test',progress=94,message='独立测试：双倍费用'))
            test['stress']=run(chosen,windows[2],stressed); persist()
        candidate=test['candidate']; baseline=test['baseline']; stress=test['stress']
        gate=result['gate']=historical_gate(candidate,baseline,stress,mode)
        for window in windows[:2]:
            key=str(chosen_idx)
            checked=window1[key] if window==windows[1] else window0.get(key)
            if checked is None: continue
            m=checked['metrics']
            if checked['invalid'] or m['total_return']<=0 or m['max_drawdown']>LIMITS[mode]['drawdown']: gate['passed']=False; gate['reasons'].append('其他历史时间段的权益、收益或亏损门槛未通过')
        state['stage']='done'; persist()
        value=dict(mode=mode,params=chosen,gate=gate,windows=windows,trials=trials,test=candidate,baseline=baseline,stress=stress,snapshot=Path(snapshot_path).name,selection_snapshot=Path(selection_path).name,seconds=round(time.monotonic()-started,2),training_workers=workers,costs=costs,engine_digest=snap.get('engine_digest'),checkpoint=cp.name)
        pipe.send(dict(done=True,result=value))
    except Exception as ex:
        try: pipe.send(dict(done=True,error=str(ex)))
        except Exception: pass
    finally: pipe.close()

class Runtime:
    def __init__(self,store,root,notifier=None,source=None,quotes=None,clock=None):
        self.store=store; self.root=Path(root); self.notifier=notifier; self.source=source or BaoSource(); self.quote_source=quotes or sina_quotes; self.clock=clock or (lambda:datetime.now(TZ))
        self.cancel=threading.Event(); self.cancel.set(); self.shutdown=threading.Event(); self.start_lock=threading.RLock(); self.data_thread=None; self.mon_thread=None; self.research_jobs={}; self.last_quote=0
        self.research_build_lock=threading.Lock()
        self.data_retry_lock=threading.RLock(); self.data_inflight=set(); self.data_wake=threading.Event()
        self.engine_digest=hashlib.sha256(Path(__file__).read_bytes()+Path(__file__).with_name('rules_v3.py').read_bytes()+Path(__file__).with_name('research_v3.py').read_bytes()).hexdigest()
        self.store.set('metrics',self.store.get('metrics',{})|dict(configured_compute_tasks=4,configured_cpu_threads=8,max_compute_tasks=4,cpu_threads=8,research_workers_per_mode=2))
        with self.store.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS paper_triggers(id TEXT PRIMARY KEY,account TEXT,time TEXT,code TEXT,side TEXT,outcome TEXT,payload TEXT)')
        if isinstance(self.source,BaoSource): self.source.progress=lambda message:self.progress(message)
    def progress(self,message,progress=None,**extra):
        old=self.store.get('work'); value=old|dict(message=message,**extra)
        if progress is not None: value['progress']=progress
        self.store.set('work',value)
    def start(self):
        with self.start_lock:
            if self.store.get('work')['active']: return dict(ok=True,message='已经在工作')
            if self.data_thread and self.data_thread.is_alive():
                # Stopping then starting again quickly resumes the one worker
                # instead of failing with "still exiting".
                self.cancel.clear(); self.data_wake.set()
                self.progress('继续准备数据',active=True,status='running',error=None)
                self.store.audit('开始工作（继续）',now())
                if not self.mon_thread or not self.mon_thread.is_alive():
                    self.mon_thread=threading.Thread(target=self.monitor_loop,daemon=True,name='quote-monitor'); self.mon_thread.start()
                return dict(ok=True,message='继续上次任务')
            self.cancel.clear(); self.data_wake.clear(); self.progress('准备交易日历和全市场数据',0,active=True,status='running',error=None)
            # A previous week's 70% must not masquerade as the current attempt,
            # but a completed result stays visible until a new run starts.
            for mode in PARAMS:
                if mode in self.research_jobs: continue
                with self.store.db() as db: done=db.execute('SELECT 1 FROM experiments WHERE mode=? LIMIT 1',(mode,)).fetchone()
                progress=self.store.get('research-progress-'+mode,{})
                if done and progress.get('phase')=='done': continue
                self.store.set('research-progress-'+mode,dict(phase='waiting',progress=0,message='等待历史检验',updated=now()))
            self.store.audit('开始工作',now())
            self.data_thread=threading.Thread(target=self.data_loop,daemon=True,name='historical-data'); self.data_thread.start()
            if not self.mon_thread or not self.mon_thread.is_alive(): self.mon_thread=threading.Thread(target=self.monitor_loop,daemon=True,name='quote-monitor'); self.mon_thread.start()
            return dict(ok=True)
    def stop(self):
        # Serialize with monitor transaction to guarantee no fills after this returns.
        with self.start_lock,self.store.lock:
            self.cancel.set(); self.data_wake.set(); self.progress('已暂停；下次点击开始继续',active=False,status='paused')
            for mode,job in list(self.research_jobs.items()):
                self.end_research_job(job)
            self.research_jobs.clear(); self.store.audit('停止工作',now())
        return dict(ok=True)
    def end_research_job(self,job):
        process=job['process']; event=job.get('cancel')
        if event is not None: event.set()
        process.join(1)
        guard=job.get('guard')
        if guard:
            measured=guard.stats(); self.store.set('metrics',self.store.get('metrics',{})|dict(research_peak_memory_mb=max(self.store.get('metrics',{}).get('research_peak_memory_mb',0),measured['peak_memory_mb'])))
            guard.close()
        if process.is_alive(): process.terminate()
        process.join(2)
        # A worker can start its one-use test before the monitor reads the marker.
        # Preserve that marker when stopping; do not integrate a result during stop.
        pipe=job['pipe']
        try:
            while pipe.poll():
                try: message=pipe.recv()
                except (EOFError,OSError): break
                if message.get('consume') and job.get('mode'): self.consume_test(job['mode'],message['consume'])
        except (OSError,ValueError): pass
        pipe.close()
    def consume_test(self,mode,day):
        old=self.store.get('consumed-'+mode)
        if not old or day>old: self.store.set('consumed-'+mode,day)
    def completed_date(self):
        clock=self.clock(); cutoff=clock.date() if clock.hour>=18 else clock.date()-timedelta(days=1)
        return max((d for d,isopen in self.store.calendar().items() if isopen and d<=cutoff.isoformat()),default=None)
    def bootstrap(self):
        clock=self.clock(); start=(clock.date()-timedelta(days=1096)).isoformat(); end=(clock.date()+timedelta(days=45)).isoformat()
        day=self.completed_date()
        if not day: day=(clock.date()-timedelta(days=1)).isoformat()
        # The terminated list alone can take half a minute; refresh it weekly.
        last_terminated=self.store.get('terminated_date')
        refresh_terminated=not last_terminated or (clock.date()-datetime.fromisoformat(last_terminated).date()).days>=6
        value=self.source.call('bootstrap',dict(start=start,end=end,day=day,terminated=refresh_terminated),self.cancel.is_set)
        with self.store.db() as db:
            for c in value['calendar']: db.execute('INSERT OR REPLACE INTO calendar VALUES(?,?)',(c['calendar_date'],int(c['is_trading_day'])))
            for r in value['securities']:
                c=r['code']; kind='mainboard' if mainboard(c) and r.get('type')=='1' else ('stock' if r.get('type')=='1' else 'other')
                db.execute('INSERT OR REPLACE INTO securities VALUES(?,?,?,?,?,?,?)',(c,r['code_name'],r.get('ipoDate',''),r.get('outDate',''),int(r.get('status')=='1'),kind,dump(r)))
            db.execute('INSERT OR IGNORE INTO securities VALUES(?,?,?,?,?,?,?)',('sh.588710','科创半导体设备ETF','2025-06-04','',1,'etf','{}'))
        self.store.set('history_start',start); self.store.set('bootstrap_date',clock.date().isoformat())
        if value.get('terminated') is not None:
            with self.store.db() as db:
                for r in value['terminated']:
                    code=r.get('code',''); ended=r.get('terminatedDate') or r.get('outDate') or r.get('terminateDate','')
                    if not mainboard(code): continue
                    existing=db.execute('SELECT 1 FROM securities WHERE code=?',(code,)).fetchone()
                    if existing: db.execute('UPDATE securities SET active=0,delisted=CASE WHEN ?!="" THEN ? ELSE delisted END WHERE code=?',(ended,ended,code))
                    else: db.execute('INSERT INTO securities VALUES(?,?,?,?,?,?,?)',(code,r.get('code_name',code),r.get('ipoDate',''),ended,0,'mainboard',dump(r)))
            self.store.set('terminated_source_complete',not bool(value.get('terminated_error')))
            if value.get('terminated_error'): self.store.audit('退市名录待补齐',value['terminated_error'])
            else: self.store.set('terminated_date',clock.date().isoformat())
        current=self.completed_date()
        # A fresh data folder only needs one seeded day; later gaps are filled in order.
        if current and not self.store.get('daily_date'):
            self.commit_daily(self.source.call('daily',dict(day=current),self.cancel.is_set),current)
    def calendar_days(self):
        return sorted(d for d,isopen in self.store.calendar().items() if isopen)
    def commit_daily(self,raw,day):
        values=validate_bars(raw)
        if any(b['date']!=day for b in values) or len({b['code'] for b in values})!=len(values): raise ValueError('全市场数据日期错配或重复')
        # A successful response that loses most of the universe is not complete.
        last=self.store.get('daily_count',0)
        # Older trading days contain only prepared main-board stocks; the daily
        # endpoint covers the whole market. Judge completeness by main-board
        # coverage rather than by the raw row count.
        with self.store.db() as db: mainboard={r[0] for r in db.execute("SELECT code FROM securities WHERE active=1 AND kind='mainboard'")}
        covered=sum(1 for b in values if b['code'] in mainboard)
        if len(values)<1000 or (mainboard and covered<len(mainboard)*.95): raise ValueError('全市场接口覆盖异常；保留原缓存')
        # Only the immediately previous trading day is comparable. A gap (for
        # example after several days without starting) changes preclose through
        # ordinary price moves; treating that as a corporate action made most of
        # the market unverified and forced a slow full refetch on every start.
        days=self.calendar_days(); previous_day=max((d for d in days if d<day),default=None)
        previous=self.store.get('daily_date')
        with self.store.db() as db:
            prior={r['code']:json.loads(r['payload']) for r in db.execute('SELECT code,payload FROM bars WHERE date=?',(previous_day,))} if previous_day else {}
            for b in values:
                db.execute('INSERT OR REPLACE INTO bars VALUES(?,?,?,?)',(b['code'],b['date'],dump(b),now()))
                if b['code'] not in prior: continue
                if abs(b['preclose']-prior[b['code']]['close'])>.001:
                    db.execute("UPDATE prepared SET error='新增公司行动待核实' WHERE code=? AND until=? AND error=''",(b['code'],previous_day))
                else: db.execute("UPDATE prepared SET until=?,actions_until=? WHERE code=? AND until=? AND error=''",(day,day,b['code'],previous_day))
        if not previous or day>previous:
            self.store.set('daily_date',day); self.store.set('daily_count',len(values))
        elif day==previous and len(values)>last: self.store.set('daily_count',len(values))
    def repair_daily_gaps(self,end):
        """Refetch recent whole-market days whose stored bars are missing or partial."""
        if not end: return 0
        last=self.store.get('daily_count',0)
        if not last: return 0
        # Repair only days whose stored coverage is actually short. Historical
        # days already hold every prepared main-board stock, so a long window
        # costs nothing there but still recovers a long absence.
        days=[d for d in self.calendar_days() if d<=end][-250:]
        if not days: return 0
        with self.store.db() as db:
            earliest=db.execute('SELECT min(date) FROM bars').fetchone()[0]
            eligible=db.execute("SELECT count(*) FROM securities WHERE active=1 AND kind='mainboard'").fetchone()[0]
            counts={r[0]:r[1] for r in db.execute("SELECT b.date,count(*) FROM bars b JOIN securities s ON s.code=b.code WHERE s.active=1 AND s.kind='mainboard' AND b.date>=? GROUP BY b.date",(days[0],))}
        if not earliest or not eligible: return 0
        floor=int(eligible*.95)
        repairs=[d for d in days if d>=earliest and counts.get(d,0)<floor]
        failed=[]
        for day in repairs:
            if self.cancel.is_set(): raise InterruptedError('已暂停')
            self.progress('补齐最近日线 · '+day)
            try: self.commit_daily(self.daily_with_retry(day),day)
            except InterruptedError: raise
            except Exception as ex: failed.append((day,str(ex)))
        if failed:
            self.store.audit('最近日线补齐未完成',dict(failed=failed))
            raise RuntimeError('最近日线补齐未完成：'+'；'.join(day+' '+reason for day,reason in failed))
        return len(repairs)
    def daily_with_retry(self,day,attempts=3):
        error=None
        for attempt in range(attempts):
            if self.cancel.is_set(): raise InterruptedError('已暂停')
            try: return self.source.call('daily',dict(day=day),self.cancel.is_set)
            except InterruptedError: raise
            except Exception as ex:
                error=ex
                if attempt<attempts-1 and self.cancel.wait(2*(attempt+1)): raise InterruptedError('已暂停')
        raise error
    def clear_gap_flags(self):
        """A mismatch across missing days is not evidence of a corporate action."""
        days=self.calendar_days(); index={d:i for i,d in enumerate(days)}
        with self.store.db() as db:
            rows=db.execute("SELECT code,until FROM prepared WHERE error='新增公司行动待核实'").fetchall()
            cleared=[]
            for r in rows:
                nxt=db.execute('SELECT min(date) FROM bars WHERE code=? AND date>?',(r['code'],r['until'])).fetchone()[0]
                if nxt in index and r['until'] in index and index[nxt]-index[r['until']]>1:
                    db.execute("UPDATE prepared SET error='' WHERE code=? AND until=?",(r['code'],r['until']))
                    cleared.append(r['code'])
        if cleared: self.store.audit('清除漏日线造成的误报',dict(count=len(cleared),reason='两次行情之间缺少交易日'))
        return len(cleared)
    def reconcile_prepared(self,end):
        """Advance prepared dates when the recent stored bars are actually complete."""
        if not end: return 0
        days=self.calendar_days(); index={d:i for i,d in enumerate(days)}
        if end not in index: return 0
        advanced=0
        with self.store.db() as db:
            rows=db.execute("SELECT p.code,p.until FROM prepared p JOIN securities s ON s.code=p.code WHERE p.error='' AND p.until<? AND s.active=1 AND s.kind='mainboard'",(end,)).fetchall()
            for r in rows:
                until=r['until']
                if until not in index: continue
                window=days[index[until]+1:index[end]+1]
                if not window: continue
                have={x[0] for x in db.execute('SELECT date FROM bars WHERE code=? AND date>? AND date<=?',(r['code'],until,end))}
                if all(d in have for d in window):
                    db.execute('UPDATE prepared SET until=? WHERE code=?',(end,r['code']))
                    advanced+=1
        if advanced: self.store.audit('日线完整后推进准备日期',dict(count=advanced,until=end))
        return advanced
    def prepare_one(self,metadata,end):
        started=time.monotonic()
        if self.cancel.is_set(): raise InterruptedError('已暂停')
        code=metadata['code']; start=max(self.store.get('history_start'),metadata['ipo'] or '1900-01-01')
        until=min(end,metadata['delisted']) if metadata['delisted'] else end
        if start>until: return
        with self.store.db() as db: prev=db.execute('SELECT * FROM prepared WHERE code=?',(code,)).fetchone()
        if prev and (prev['error'] or '').startswith('内部交易日线缺失'): begin=start
        else:
            # A delisted stock's requested end is earlier than its last checked
            # date; clamp the incremental window to that end or the source
            # rejects start>end and the sample is retried forever.
            anchor=min(until,prev['until']) if prev and prev['until'] else until
            begin=start if not prev else max(start,(datetime.fromisoformat(anchor).date()-timedelta(days=10)).isoformat())
            if begin>until: begin=start
        result=self.source.call('history',dict(code=code,start=begin,end=until,actions=True),self.cancel.is_set)
        bars=validate_bars(result['bars'],code)
        combined={b['date']:b for b in self.store.bars(code)}; combined.update({b['date']:b for b in bars})
        expected={d for d,v in self.store.calendar().items() if v and start<=d<=bars[-1]['date']}
        if expected-set(combined): raise ValueError('内部交易日线缺失，原缓存保留')
        if metadata['active'] and bars[-1]['date']!=end: raise ValueError('最新日线尚未提供')
        events=[parse_action(r) for r in result['dividends']]; events=[e for e in events if e]
        with self.store.db() as db:
            for b in bars: db.execute('INSERT OR REPLACE INTO bars VALUES(?,?,?,?)',(code,b['date'],dump(b),now()))
            for e in events: db.execute('INSERT OR REPLACE INTO actions VALUES(?,?,?)',(code,e['date'],dump(e)))
            db.execute('INSERT OR REPLACE INTO prepared VALUES(?,?,?,?,?)',(code,end,start,'',end))
        metrics=self.store.get('metrics',{}); metrics.update(last_security_seconds=round(time.monotonic()-started,2),data_elapsed_seconds=round(time.monotonic()-self.data_started,2) if hasattr(self,'data_started') else 0); self.store.set('metrics',metrics)
    def request_data_retry(self):
        """Queue current failures without discarding cache or starting stopped work."""
        with self.data_retry_lock:
            with self.store.db() as db: failed=[r[0] for r in db.execute("SELECT code FROM prepared WHERE error!='' ORDER BY code")]
            queue=self.store.get('data_retry_queue',[]); known=set(queue)|self.data_inflight
            added=[c for c in failed if c not in known]; queue+=added
            self.store.set('data_retry_queue',queue)
            active=bool(self.store.get('work')['active'])
            if active and queue: self.data_wake.set()
            if added: self.store.audit('请求重试失败数据',dict(codes=added,active=active))
            return dict(queued=len(added),count=len(queue),active=active)
    def next_data_retry(self):
        with self.data_retry_lock:
            queue=self.store.get('data_retry_queue',[]); retained=[]; selected=None
            with self.store.db() as db:
                for code in queue:
                    status=db.execute('SELECT error FROM prepared WHERE code=?',(code,)).fetchone()
                    if not status or not status['error']: continue
                    retained.append(code)
                    if selected is None and code not in self.data_inflight:
                        row=db.execute('SELECT * FROM securities WHERE code=?',(code,)).fetchone()
                        if row: selected=dict(row)
            if retained!=queue: self.store.set('data_retry_queue',retained)
            return selected
    def finish_data_attempt(self,code,completed):
        with self.data_retry_lock:
            self.data_inflight.discard(code)
            if completed:
                queue=self.store.get('data_retry_queue',[])
                if code in queue: self.store.set('data_retry_queue',[c for c in queue if c!=code])
    def pending_securities(self,end):
        """Active symbols follow the latest day; a prepared delisted sample is
        complete at its delisting date and must not be downloaded every day."""
        sql=("SELECT s.* FROM securities s LEFT JOIN prepared p ON s.code=p.code "
             "WHERE (s.kind='mainboard' OR s.code IN ('sh.588710','sh.000300')) "
             "AND (s.delisted='' OR s.delisted>=?) "
             "AND ((s.active=1 AND (p.until IS NULL OR p.until!=? OR p.error!='')) "
             "OR (s.active=0 AND (p.until IS NULL OR p.error!='' OR (s.delisted!='' AND p.until<s.delisted)))) "
             "ORDER BY CASE WHEN s.code IN ('sh.000300','sh.588710') THEN 0 WHEN s.active=1 THEN 1 ELSE 2 END,s.code")
        with self.store.db() as db: return [dict(r) for r in db.execute(sql,(self.store.get('history_start'),end))]
    def data_loop(self):
        started=time.monotonic()
        self.data_started=started
        try:
            for attempt in range(3):
                try:
                    if self.store.get('bootstrap_date')!=self.clock().date().isoformat() or not self.store.get('daily_date'): self.bootstrap()
                    break
                except InterruptedError: raise
                except Exception as ex:
                    if attempt==2: raise
                    self.progress(f'连接暂未完成，稍后重试（{attempt+1}/3）：{ex}')
                    if self.cancel.wait(10*(attempt+1)): raise InterruptedError()
            end=self.completed_date()
            if not end: raise ValueError('尚无已完成交易日')
            gaps_ready=False; plans_ready=False; gap_failures=0
            while not self.cancel.is_set():
                if self.store.get('bootstrap_date')!=self.clock().date().isoformat(): self.bootstrap()
                end=self.completed_date()
                if not end: raise ValueError('尚无已完成交易日')
                if self.store.get('daily_date')!=end:
                    self.progress('补齐全市场最新日线')
                    last=self.store.get('daily_date')
                    # Never replay a whole calendar when the first daily seed is missing.
                    days=[end] if not last else sorted(d for d,v in self.store.calendar().items() if v and d>last and d<=end)
                    try:
                        for day in days: self.commit_daily(self.daily_with_retry(day),day)
                    except InterruptedError: raise
                    except Exception as ex:
                        self.progress('最新日线补齐暂未完成，稍后自动重试：'+str(ex),status='running')
                        if self.cancel.wait(30): break
                        continue
                if not gaps_ready:
                    self.progress('核对最近日线完整性')
                    try:
                        cleared=self.clear_gap_flags(); repaired=self.repair_daily_gaps(end); advanced=self.reconcile_prepared(end)
                        if cleared or repaired or advanced: self.store.audit('日线自动修复',dict(cleared=cleared,repaired=repaired,advanced=advanced,asof=end))
                        gaps_ready=True; gap_failures=0
                    except InterruptedError: raise
                    except Exception as ex:
                        # A transient partial response must not end the whole work
                        # session; retry instead of making the user click again.
                        gap_failures+=1
                        if gap_failures<3:
                            self.progress(f'最近日线补齐暂未完成（第{gap_failures}次），稍后自动重试：'+str(ex),status='running')
                            if self.cancel.wait(30): break
                            continue
                        # Persistent whole-day gaps fall back to per-stock history.
                        self.store.audit('最近日线改由逐股补齐',dict(attempts=gap_failures,error=str(ex)))
                        self.progress('最近日线多次未补齐，改为逐股补齐历史',status='running')
                        gaps_ready=True
                if not plans_ready and self.store.coverage()['complete']:
                    # User-facing plans do not wait for research-only samples.
                    self.refresh_completed_marks(end); self.prepare_real_exits(); self.make_plans(); plans_ready=True
                pending=self.pending_securities(end)
                total=len(pending)
                completed_codes=set(); submitted=set(); iterator=iter(pending)
                # BaoStock sessions are already serialized; submitting one query at
                # a time lets an explicit retry precede untouched normal symbols.
                with ThreadPoolExecutor(max_workers=1,thread_name_prefix='history-batch') as executor:
                    futures={}
                    def submit_next():
                        if self.cancel.is_set(): return
                        m=self.next_data_retry()
                        if m is not None: self.progress('优先重试失败数据 · '+m['name'])
                        else:
                            m=next(iterator,None)
                            while m is not None and m['code'] in submitted: m=next(iterator,None)
                        if m is not None:
                            with self.data_retry_lock: self.data_inflight.add(m['code'])
                            submitted.add(m['code']); futures[executor.submit(self.prepare_one,m,end)]=m
                    submit_next()
                    while futures:
                        if self.cancel.is_set(): raise InterruptedError()
                        done,_=wait(futures,timeout=.2,return_when=FIRST_COMPLETED)
                        for future in done:
                            m=futures.pop(future); finished=False
                            try: future.result()
                            except InterruptedError: raise
                            except Exception as ex:
                                with self.store.db() as db: db.execute('INSERT OR REPLACE INTO prepared VALUES(?,?,?,?,?)',(m['code'],end,self.store.get('history_start'),str(ex),''))
                            else: finished=True
                            finally:
                                # A handled failure is an attempted retry too; only
                                # cancellation preserves the request for recovery.
                                if not self.cancel.is_set(): finished=True
                                self.finish_data_attempt(m['code'],finished)
                            completed_codes.add(m['code']); completed=len(completed_codes)
                            prefix='准备历史' if m['active'] else '补齐研究样本'
                            self.progress(f"{prefix} · 本批完成 {completed}/{total} · {m['name']}",min(100,round(completed/max(1,total)*100)))
                            self.prepare_real_exits(); submit_next()
                            if not plans_ready and self.store.coverage()['complete']:
                                self.refresh_completed_marks(end); self.make_plans(); plans_ready=True
                self.refresh_completed_marks(end); self.make_plans(); self.prepare_real_exits(); plans_ready=True
                coverage=self.store.coverage()
                if not coverage['complete']:
                    gaps_ready=False
                    percent=min(99,round(coverage['prepared']/max(1,coverage['eligible'])*100))
                    self.progress('历史数据仍不完整，稍后继续补齐',percent)
                else:
                    self.progress('持续观察价格' if self.session()=='trading' else '历史整理完成；等待新行情或策略比较',100)
                try: self.research_tick()
                except InterruptedError: raise
                except Exception as ex:
                    # Research is optional background work and must not stop quotes or plans.
                    self.store.audit('策略比较暂缓',str(ex)); self.store.set('research_wait','策略比较暂缓：'+str(ex))
                metrics=self.store.get('metrics',{}); metrics.setdefault('max_compute_tasks',4); metrics.setdefault('cpu_threads',8)
                self.store.set('metrics',metrics|dict(data_elapsed_seconds=round(time.monotonic()-started,2)))
                self.data_wake.wait(60); self.data_wake.clear()
                if self.cancel.is_set(): break
        except InterruptedError: pass
        except Exception as ex: self.progress('数据准备未完成，点击开始重试：'+str(ex),active=False,status='error',error=str(ex)); self.cancel.set()
        finally:
            with self.data_retry_lock: self.data_inflight.clear()
            if self.cancel.is_set() and self.store.get('work')['status']!='error': self.progress('已暂停；下次点击开始继续',active=False,status='paused')
            # A fast stop→start can clear cancellation while this worker is
            # unwinding; keep exactly one data worker instead of leaving the app
            # marked "working" with no loop behind it.
            if not self.cancel.is_set() and not self.shutdown.is_set() and self.store.get('work')['active'] and threading.current_thread() is self.data_thread:
                self.data_thread=threading.Thread(target=self.data_loop,daemon=True,name='historical-data'); self.data_thread.start()
    def session(self): return trading_session(self.clock(),self.store.calendar())
    def accounts_to_monitor(self):
        result=[('paper-'+m,m,self.store.strategy(m)) for m in PARAMS]
        with self.store.db() as db: candidates=[dict(r) for r in db.execute("SELECT * FROM strategies WHERE status='challenger'")]
        for c in candidates:
            c['payload']=json.loads(c['payload']); result.append(('shadow-'+c['id'],c['mode'],c))
            control=c['payload']['control']; result.append(('control-'+c['id'],c['mode'],dict(id=c['payload']['baseline'],payload=dict(params=control))))
        return result
    def make_plans(self):
        coverage=self.store.coverage()
        if not coverage['complete']: return
        asof=self.store.get('daily_date'); day=min((d for d,v in self.store.calendar().items() if v and d>asof),default=None)
        if not day: return
        with self.store.db() as db: securities=[dict(r) for r in db.execute("SELECT * FROM securities WHERE active=1 AND kind='mainboard'")]
        entries=self.accounts_to_monitor()
        benchmark=self.store.bars('sh.000300',asof,120)
        if self.store.get('settings')['stage']=='real': entries += [('real',m,self.store.strategy(m)) for m in PARAMS]
        for account,mode,strategy in entries:
            if self.store.latest_plan(account,day,mode): continue
            a=self.store.account(account); equity=self.store.equity_value(a); used=equity-a['cash']; candidates=[]; excluded={}; detail=[]
            params=strategy['payload']['params']; context=market_context(benchmark,params,mode)
            history={c:self.store.bars(c,asof,100) for c in a['positions']}
            for m in securities:
                bs=self.store.bars(m['code'],asof,100); history[m['code']]=bs; reason=None
                if not bs or bs[-1]['date']!=asof: reason='最新数据不足'
                elif bs[-1]['tradestatus']!='1': reason='停牌'
                elif bs[-1]['isST']!='0': reason='风险警示'
                elif len(bs)<strategy['payload']['params']['slow']: reason='历史不足'
                elif m['code'] in a['positions']: reason='已有持仓'
                elif a['halt']: reason='达到暂停新增买入条件'
                elif not context['allow_new']: reason=context['reason']
                else:
                    value=make_plan(m['code'],m['name'],bs,params,mode,equity,a['cash'],used,strategy['id'],self.store.get('settings')['costs'],benchmark=benchmark,exposure_scale=context['exposure_scale'])
                    if not value: reason='未符合策略规则'
                    elif value['quantity']<100: reason='资金或整手限制'
                    else: candidates.append(value)
                if reason:
                    excluded[reason]=excluded.get(reason,0)+1
                    if reason!='未符合策略规则' and len(detail)<100: detail.append(dict(code=m['code'],name=m['name'],reason=reason))
            if params.get('quality'):
                candidates,rejected=select_diverse(candidates,history,mode,held_codes=list(a['positions']))
                for row in rejected:
                    excluded[row['reason']]=excluded.get(row['reason'],0)+1
                    if len(detail)<100: detail.append(row)
            items=sorted(candidates,key=lambda p:(-p['score'],p['code']))[:min(5,max(0,LIMITS[mode]['slots']-len(a['positions'])))]
            # Reserve cash across all same-day candidates; individual plans cannot overspend in aggregate.
            remaining=a['cash']; allocated=used; funded=[]
            frozen_costs=copy.deepcopy(self.store.get('settings')['costs'])
            for p in items:
                p['quantity']=quantity(p['buy_high'],p['stop'],equity,remaining,allocated,mode,self.store.get('settings')['costs'],exposure_scale=context['exposure_scale'])
                if not p['quantity']: continue
                amount=p['quantity']*p['buy_high']; remaining-=amount+charge(amount,'buy',day,self.store.get('settings')['costs']); allocated+=amount
                p['costs']=frozen_costs; p['engine_digest']=self.engine_digest; funded.append(p)
            self.store.plan_save(account,mode,day,dict(asof=asof,version=strategy['id'],items=funded,excluded=excluded,details=detail,screened=len(securities),status='ready',market=context,costs=frozen_costs,engine_digest=self.engine_digest))
    def refresh_completed_marks(self,day):
        """Completed daily closes value holdings; they never masquerade as live quotes."""
        if self.session() in ('trading','lunch','auction'): return
        with self.store.lock:
            entries=self.accounts_to_monitor()+[('real',None,None)]
            for account,mode,_ in entries:
                a=self.store.account(account); marks={}
                for c,h in a['positions'].items():
                    bs=self.store.bars(c,day,1)
                    if bs and bs[-1]['date']==day and h['date']<=day: marks[c]=dict(price=bs[-1]['close'],source='completed_daily_close',date=day)
                if marks:
                    self.store.apply_actions(account,day); self.store.update_marks(account,marks,day,mode,observe=False)
            self.store.set('closing_marks',dict(date=day,source='BaoStock已完成日线收盘价'))
    def prepare_real_exits(self):
        with self.store.lock:
            a=self.store.account('real'); mode=self.store.get('settings')['mode']; p=self.store.strategy(mode)['payload']['params']
            for c,h in a['positions'].items():
                if h.get('stop') is not None: continue
                bars=self.store.bars(c,self.completed_date(),30)
                if len(bars)<15: continue
                atr=sum(max(b['high']-b['low'],abs(b['high']-b['preclose']),abs(b['low']-b['preclose'])) for b in bars[-14:])/14
                ref=bars[-1]['close']; dist=max(ref*.025,min(ref*.15,atr*p['atr_multiple'])); tick=.001 if h['kind']=='etf' else .01
                h.update(stop=rounded(ref-dist,tick),target=rounded(ref+2*dist,tick),distance=dist,peak=ref,hold=p['hold'],observation_start=self.clock().date().isoformat(),rule_version=self.store.strategy(mode)['id'],exit_note='以导入后的已完成行情建立观察条件，不以亏损成本推测回本日期')
            self.store.save_account('real',a)
    def notify(self,key,title,message,plan_id=None):
        payload=dict(title=title,message=message,plan_id=plan_id)
        if self.store.notify(key,payload) and self.store.get('settings')['notifications'] and self.notifier:
            try: self.notifier.notify(title,message,plan_id)
            except Exception as ex: self.store.audit('桌面提醒发送失败',dict(key=key,error=str(ex)))
    def record_trigger(self,account,code,side,quote,outcome,context):
        payload=dict(account=account,code=code,side=side,quote=copy.deepcopy(quote),costs=copy.deepcopy(self.store.get('settings')['costs']),context=context,outcome=outcome,observed=self.clock().isoformat(),execution_engine_digest=self.engine_digest)
        key=hashlib.sha256(dump(dict(account=account,code=code,side=side,source=quote['source_time'],plan=context.get('id'),reason=context.get('reason'),outcome=outcome)).encode()).hexdigest()
        with self.store.db() as db: db.execute('INSERT OR IGNORE INTO paper_triggers VALUES(?,?,?,?,?,?,?)',(key,account,payload['observed'],code,side,outcome,dump(payload)))
    def position_plan(self,h):
        ident=h.get('plan_id'); context=dict(id=ident,stop=h.get('stop'),target=h.get('target'),hold=h.get('hold'),date=h['date'],available=h.get('available',0))
        if ident:
            with self.store.db() as db: row=db.execute('SELECT payload FROM plans WHERE id=?',(ident.split(':')[0],)).fetchone()
            if row:
                value=json.loads(row[0]); context.update(version=value['version'] if 'version' in value else None,original_plan=next((p for p in value['items'] if p['id']==ident),None))
        return context
    @staticmethod
    def quote_capacity(q,side,costs):
        capacity=float(q['volume'])*costs['participation']; field='ask_volume' if side=='buy' else 'bid_volume'
        if field in q:
            depth=float(q[field])
            if not math.isfinite(depth) or depth<=0: return 0
            capacity=min(capacity,depth)
        elif q.get('source')=='新浪公开报价': return 0
        return max(0,int(capacity))
    def monitor_once(self,force=False):
        if self.cancel.is_set(): return
        entries=self.accounts_to_monitor(); codes=set(self.store.account('real')['positions'])
        day=self.clock().date().isoformat()
        for account,mode,strategy in entries:
            codes.update(self.store.account(account)['positions'])
            plan=self.store.latest_plan(account,day)
            if plan: codes.update(p['code'] for p in plan['items'])
        if self.store.get('settings')['stage']=='real':
            real_plan=self.store.latest_plan('real',day,self.store.get('settings')['mode'])
            if real_plan: codes.update(p['code'] for p in real_plan['items'])
        try: quotes=self.quote_source(codes)
        except Exception as ex:
            self.store.set('quote_health',dict(error=str(ex),fetched=now(),requested=len(codes),received=0,fresh=0,session=self.session(),status='paused',message='行情连接中断，价格触发和模拟成交已暂停'))
            self.progress('行情连接中断；暂停价格触发，历史整理可继续',status='degraded')
            return
        clock=self.clock(); session=self.session()
        sample_ids={}
        with self.store.db() as db:
            for c,q in quotes.items():
                raw=dump(q); fingerprint=hashlib.sha256(dump({k:v for k,v in q.items() if k!='fetched'}).encode()).hexdigest(); sample_ids[c]=fingerprint
                db.execute('INSERT OR REPLACE INTO quotes VALUES(?,?)',(c,raw))
                db.execute('INSERT OR IGNORE INTO quote_samples VALUES(?,?,?,?,?)',(fingerprint,c,q['source_time'],q.get('fetched',now()),raw))
        fresh={c:copy.deepcopy(q)|dict(quote_sample_id=sample_ids[c]) for c,q in quotes.items() if fresh_quote(q,clock)}
        # A source status/name change invalidates a buy even if yesterday's ST flag was clear.
        for c,q in fresh.items(): q['risk_warning']=mainboard(c) and ('ST' in q.get('name','').upper() or '退' in q.get('name',''))
        complete=bool(codes) and codes<=set(fresh)
        message='价格观察正常' if session=='trading' and complete else '部分行情缺失或超过120秒，暂停受影响股票触发' if session=='trading' else SESSION_NAMES[session]+'；暂停模拟成交'
        self.store.set('quote_health',dict(fetched=now(),requested=len(codes),received=len(quotes),fresh=len(fresh),session=session,status='healthy' if complete else 'paused',message=message))
        if self.store.get('work')['status']=='degraded' and complete: self.progress('行情连接已恢复',status='running')
        # Closed/lunch/auction calls only update display, never paper fills or trigger alerts.
        if session!='trading': return
        with self.store.lock:
            if self.cancel.is_set(): return
            costs=self.store.get('settings')['costs']
            for account,mode,strategy in entries:
                self.store.apply_actions(account,day)
                prior=self.store.account(account); held_healthy=all(c in fresh for c in prior['positions'])
                plan_codes={p['code'] for p in (self.store.latest_plan(account,day) or {}).get('items',[])}
                observe=bool(fresh) and held_healthy and all(c in fresh for c in plan_codes)
                a=self.store.update_marks(account,fresh,day,mode,observe=observe)
                for c,h in list(a['positions'].items()):
                    q=fresh.get(c)
                    if not q or h.get('stop') is None: continue
                    age=sum(1 for d,v in self.store.calendar().items() if v and h['date']<d<=day)
                    reason='止损' if q['price']<=h['stop'] else '止盈' if q['price']>=h['target'] else ('持有期限' if age>=h['hold'] else None)
                    if not reason: continue
                    context=self.position_plan(h)|dict(reason=reason)
                    if account.startswith('paper-'): self.notify(account+':'+(h.get('plan_id') or h['date'])+':'+c+':'+reason,h['name']+' 达到'+reason,f"现价 {q['price']:.3f} 元",h.get('plan_id'))
                    if not h['available']:
                        self.record_trigger(account,c,'sell',q,'当天买入尚不可卖',context); continue
                    price=executable(q,'sell',c,day,costs,q.get('risk_warning',False))
                    capacity=self.quote_capacity(q,'sell',costs); maximum=min(h['available'],capacity)
                    qty=maximum if maximum==h['quantity'] else int(maximum/100)*100
                    if price and qty:
                        filled=self.store.fill(account,'sell',c,price,qty,day,charge(price*qty,'sell',day,costs),context,key=account+':sell:'+c+':'+h['date']+':'+q['quote_sample_id'],source_quote=q)
                        self.record_trigger(account,c,'sell',q,'模拟成交' if filled else '重复成交已拦截',context|dict(price=price,quantity=qty))
                    else: self.record_trigger(account,c,'sell',q,'盘口或成交量条件未满足',context)
                plan=self.store.latest_plan(account,day)
                if not plan: continue
                # Healthy exits remain possible even when another holding is stale.
                # New positions require a reliable whole-account valuation/list.
                if not held_healthy or not all(c in fresh for c in plan_codes): continue
                with self.store.db() as db: today_fills=[json.loads(r[0]) for r in db.execute('SELECT payload FROM paper_fills WHERE account=? AND date=?',(account,day))]
                sold_today={f['code'] for f in today_fills if f['side']=='sell'}
                for p in plan['items']:
                    q=fresh.get(p['code']); a=self.store.account(account)
                    if not q or q.get('risk_warning') or a['halt'] or p['code'] in a['positions'] or len(a['positions'])>=LIMITS[mode]['slots']: continue
                    if p.get('signal') and abs(q['preclose']-p['signal']['close'])>.001: continue
                    price=executable(q,'buy',p['code'],day,costs)
                    if not p['buy_low']<=q['price']<=p['buy_high']: continue
                    if not price or not p['buy_low']<=price<=p['buy_high']:
                        self.record_trigger(account,p['code'],'buy',q,'盘口、价格限制或滑点条件未满足',p); continue
                    if p['code'] in sold_today:
                        self.record_trigger(account,p['code'],'buy',q,'当天已退出，等待下一交易日计划',p); continue
                    if p.get('costs',plan.get('costs',costs))!=costs:
                        self.record_trigger(account,p['code'],'buy',q,'费用模型已变化，冻结计划不能新增买入',p); continue
                    eq=self.store.equity_value(a); qty=min(p['quantity'],quantity(price,p['stop'],eq,a['cash'],eq-a['cash'],mode,costs,q['volume'],exposure_scale=p.get('exposure_scale',1.)),int(self.quote_capacity(q,'buy',costs)/100)*100)
                    if not qty:
                        self.record_trigger(account,p['code'],'buy',q,'现金、风险或盘口可用股数不足',p); continue
                    filled=self.store.fill(account,'buy',p['code'],price,qty,day,charge(price*qty,'buy',day,costs),p,key=account+':buy:'+p['id'],source_quote=q)
                    self.record_trigger(account,p['code'],'buy',q,'模拟成交' if filled else '重复成交已拦截',p|dict(price=price,quantity=qty))
                    if filled and account.startswith('paper-'): self.notify(p['id']+':entry',p['name']+' 达到买入范围',f"模拟买入 {qty} 股，{price:.2f} 元",p['id'])
            self.store.apply_actions('real',day); real=self.store.update_marks('real',fresh,day)
            if self.store.get('settings')['stage']=='real':
                for c,h in real['positions'].items():
                    q=fresh.get(c)
                    if not q or h.get('stop') is None: continue
                    age=sum(1 for d,v in self.store.calendar().items() if v and h.get('observation_start',h['date'])<d<=day)
                    reason='止损' if q['price']<=h['stop'] else '止盈' if q['price']>=h['target'] else ('观察期限' if h.get('hold') and age>=h['hold'] else None)
                    if reason: self.notify('real:'+h['date']+':'+c+':'+reason,h['name']+' 实盘退出提醒',f"现价 {q['price']:.3f} 元，请在券商软件手动处理",'page:real')
                plan=self.store.latest_plan('real',day,self.store.get('settings')['mode'])
                if plan:
                    codes.update(p['code'] for p in plan['items'])
                    for p in plan['items']:
                        q=fresh.get(p['code'])
                        if not q or q.get('risk_warning') or not p['buy_low']<=q['price']<=p['buy_high']: continue
                        if p.get('signal') and abs(q['preclose']-p['signal']['close'])>.001: continue
                        price=executable(q,'buy',p['code'],day,costs)
                        if price and p['buy_low']<=price<=p['buy_high']: self.notify('real:'+p['id']+':entry',p['name']+' 实盘买入条件提醒',f"现价 {q['price']:.2f} 元，请核对实际现金后手动操作",p['id'])
            for account,mode,strategy in entries:
                if account.startswith('shadow-'): self.review_forward(strategy)
            if self.store.readiness()['ready']: self.notify('ready','模拟观察达到门槛','请打开模拟表现查看结果；实盘参考仍需你手动启用')
    def monitor_loop(self):
        while not self.shutdown.is_set():
            if not self.cancel.is_set():
                try:
                    if time.monotonic()-self.last_quote>=self.store.get('settings')['quote_interval']:
                        self.last_quote=time.monotonic(); self.monitor_once()
                    self.poll_research()
                except Exception as ex:
                    self.store.set('quote_health',dict(error=str(ex),fetched=now(),fresh=0,session=self.session()))
                    self.stop(); self.progress('工作出现错误，已暂停：'+str(ex),active=False,status='error',error=str(ex))
            self.shutdown.wait(1)
    def research_tick(self):
        # Large immutable snapshots must not hold the lock needed by Stop/quotes.
        if not self.research_build_lock.acquire(blocking=False): return
        try: self._research_tick()
        finally: self.research_build_lock.release()
    def research_digest(self):
        # Scheduler and UI changes must not invalidate an in-progress comparison.
        return hashlib.sha256(Path(__file__).with_name('rules_v3.py').read_bytes()+Path(__file__).with_name('research_v3.py').read_bytes()).hexdigest()
    def research_key(self,mode,design,consumed,costs):
        payload=dict(mode=mode,grid=hashlib.sha256(dump(candidate_grid(mode)).encode()).hexdigest(),research=self.research_digest(),
                     windows=[list(w) for w in design['windows']],consumed=consumed,costs=costs,history_start=self.store.get('history_start'))
        return hashlib.sha256(dump(payload).encode()).hexdigest()
    def experiment_for_key(self,mode,key):
        with self.store.db() as db: rows=db.execute("SELECT created,payload FROM experiments WHERE mode=? ORDER BY rowid DESC LIMIT 50",(mode,)).fetchall()
        for row in rows:
            payload=json.loads(row['payload'])
            if payload.get('key')==key: return row['created'],payload
        return None,None
    def create_research_design(self,days):
        cut1=int(len(days)*.6); cut2=int(len(days)*.8)
        return dict(windows=[(days[60],days[cut1-1]),(days[cut1],days[cut2-1]),(days[cut2],days[-1])],selection_snapshot='',created=now())
    def request_research(self,force=False):
        if not self.store.coverage()['complete']: return dict(ok=False,message='历史数据尚未准备完整，先点开始工作补齐')
        if not self.store.get('work')['active']: return dict(ok=False,message='先在首页点“开始工作”，再运行历史检验')
        self.store.set('research_requested',True)
        self.store.set('research_force',bool(force))
        self.store.set('research_retry_after',None)
        queued=self.session() in ('trading','lunch','auction')
        message='已排队，闭市后自动开始；请保持软件运行' if queued else '已开始检验；请保持软件运行，停止工作会保存断点'
        self.store.set('research_wait','已排队，闭市后开始历史检验' if queued else '已请求检验，准备历史快照')
        self.store.audit('请求检验策略',dict(force=bool(force),queued=queued))
        self.data_wake.set()
        return dict(ok=True,message=message,queued=queued)
    def _research_tick(self):
        if self.cancel.is_set(): return
        self.poll_research()
        if self.session() in ('trading','lunch','auction') or not self.store.coverage()['complete']: return
        if not self.store.get('terminated_source_complete',False): self.store.set('research_wait','退市名录尚未核实完整，暂停升级验证'); return
        with self.store.db() as db:
            missing=db.execute("SELECT count(*) FROM securities s LEFT JOIN prepared p ON p.code=s.code WHERE s.kind='mainboard' AND s.active=0 AND s.delisted>=? AND (p.until IS NULL OR p.error!='')",(self.store.get('history_start'),)).fetchone()[0]
        if missing: self.store.set('research_wait','退市历史样本尚未准备完成'); return
        clock=self.clock(); force=bool(self.store.get('research_force')); requested=bool(self.store.get('research_requested'))
        retry_after=self.store.get('research_retry_after')
        if not force and not requested and retry_after:
            try: waiting=datetime.fromisoformat(retry_after)
            except (TypeError,ValueError): waiting=None
            if waiting and clock<waiting:
                self.store.set('research_wait','策略比较暂缓，稍后自动重试'); return
        week=clock.strftime('%G-W%V'); selected_workers=None
        with self.store.db() as db:
            days=[r[0] for r in db.execute('SELECT DISTINCT date FROM bars ORDER BY date')]
            meta={r['code']:dict(r) for r in db.execute("SELECT * FROM securities WHERE kind='mainboard' AND (delisted='' OR delisted>=?)",(self.store.get('history_start'),))}
            acts={}
            for r in db.execute('SELECT * FROM actions'): acts.setdefault(r['code'],{})[r['date']]=json.loads(r['payload'])
        if len(days)<350: self.store.set('research_wait','至少需要350个交易日供按时间划分验证'); return
        costs=self.store.get('settings')['costs']
        for mode in PARAMS:
            with self.store.db() as db: challenger=db.execute("SELECT 1 FROM strategies WHERE mode=? AND status='challenger'",(mode,)).fetchone()
            if challenger or mode in self.research_jobs: continue
            consumed=self.store.get('consumed-'+mode)
            if consumed and len([d for d in days if d>consumed])<20:
                self.store.set('research_wait','等待至少20个新的不重叠测试交易日；旧测试不会用于调参'); continue
            design=self.store.get('research_design-'+mode)
            if not design:
                design=self.create_research_design(days); self.store.set('research_design-'+mode,design)
            key=self.research_key(mode,design,consumed,costs)
            experiment_at,experiment=self.experiment_for_key(mode,key)
            if experiment:
                test_end=max(w[1] for w in design['windows'])
                fresh=[d for d in days if d>test_end]
                if len(fresh)>=20:
                    design=self.create_research_design(days); self.store.set('research_design-'+mode,design)
                    key=self.research_key(mode,design,consumed,costs)
                    self.store.audit('新交易日足够，开始新一轮检验',dict(mode=mode,days=len(fresh)))
                else:
                    self.store.set('research_wait','当前规则和历史区间已完成检验；等待新的未使用交易日或手动重新检验')
                    if force or requested: self.store.set('research_wait','没有足够新的交易日；相同历史区间不会重复计算，避免制造虚假成绩')
                    continue
            self.store.set('research_retry_after',None)
            file=self.root/'outputs'/('v3-research-data-'+key[:24]+'.json'); cp=self.root/'work'/f'research-{mode}-{key[:16]}.json'
            selection_path=design.get('selection_snapshot') or ''
            if selection_path and not Path(selection_path).is_file(): selection_path=''
            if not selection_path: selection_path=str(file)
            byte_count=file.stat().st_size if file.is_file() else 0
            if not byte_count:
                self.store.set('research-progress-'+mode,dict(phase='snapshot',progress=1,message='整理历史数据快照',updated=now()))
                ds={}
                for c in meta:
                    if self.cancel.is_set(): return
                    bs=self.store.bars(c,days[-1])
                    if bs: ds[c]=bs
                if self.cancel.is_set(): return
                snapshot=dict(dataset=ds,metadata=meta,actions=acts,benchmark=self.store.bars('sh.000300',days[-1]),costs=costs,consumed=consumed,
                              design=dict(windows=design['windows'],selection_snapshot=selection_path),selection_snapshot=selection_path,
                              grid_digest=hashlib.sha256(dump(candidate_grid(mode)).encode()).hexdigest(),engine_digest=self.research_digest())
                raw=dump(snapshot); encoded=raw.encode(); byte_count=len(encoded); del encoded
                if not file.exists():
                    temp=file.with_suffix('.tmp'); temp.write_text(raw,encoding='utf-8'); os.replace(temp,file)
                del snapshot,raw,ds
            # The first design keeps its frozen selection snapshot; a new test
            # window must never rewrite the data used to choose parameters.
            if not design.get('selection_snapshot'):
                design['selection_snapshot']=selection_path; self.store.set('research_design-'+mode,design)
            self.store.set('research_cache-'+mode,dict(key=key,snapshot=str(file),checkpoint=str(cp),created=now(),bytes=byte_count))
            if selected_workers is None:
                # Parsed snapshots dominate memory; keep a conservative reserve for quotes/UI.
                free=available_memory()
                if free<byte_count*12+1024**3:
                    self.store.set('research_wait','可用内存不足，历史比较暂缓；价格观察继续')
                    self.store.set('metrics',self.store.get('metrics',{})|dict(max_compute_tasks=2,cpu_threads=4,research_available_memory_mb=round(free/1048576,2),research_snapshot_mb=round(byte_count/1048576,2)))
                    return
                selected_workers=2 if free>byte_count*48+2*1024**3 else 1
            selected_workers=self.launch_research_job(mode,file,cp=cp,week=week,workers=selected_workers,key=key) or selected_workers
        if requested or force:
            self.store.set('research_requested',False); self.store.set('research_force',False)
    def launch_research_job(self,mode,file,cp,week,workers,key=None):
        with self.start_lock:
            if self.cancel.is_set() or mode in self.research_jobs: return
            ctx=mp.get_context('spawn'); parent,child=ctx.Pipe(False); version=self.store.strategy(mode); event=ctx.Event(); gate=ctx.Event()
            def launch(width):
                process=ctx.Process(target=research_worker,args=(child,str(file),mode,version['payload']['params'],str(cp),event,width,gate),daemon=False); process.start()
                try: guard=ResearchProcessGuard(process.pid)
                except OSError:
                    if width>1:
                        process.terminate(); process.join(2); raise
                    guard=None
                return process,guard
            try: process,guard=launch(workers)
            except OSError as ex:
                for other_mode,other_job in list(self.research_jobs.items()):
                    self.end_research_job(other_job); self.research_jobs.pop(other_mode,None)
                workers=1; self.store.audit('研究采用单进程模式',str(ex)); process,guard=launch(1)
            child.close(); gate.set()
            self.research_jobs[mode]=dict(mode=mode,process=process,pipe=parent,cancel=event,guard=guard,workers=workers,phase='training',snapshot=str(file),baseline=version['id'],week=week,key=key,checkpoint=str(cp),started=time.monotonic())
            capacity=workers*2
            self.store.set('metrics',self.store.get('metrics',{})|dict(max_compute_tasks=capacity,cpu_threads=capacity*2,research_workers_per_mode=workers))
            self.store.set('research-progress-'+mode,dict(phase='training',workers=workers,progress=0,message='启动策略比较',updated=now()))
            self.store.set('research_wait',f'比较策略中；最多同时{capacity}个计算任务')
            return workers
    def poll_research(self):
        with self.start_lock: self._poll_research()
    def _poll_research(self):
        for mode,job in list(self.research_jobs.items()):
            pipe=job['pipe']
            while True:
                try:
                    if not pipe.poll():
                        if job['process'].is_alive(): break
                        message=dict(done=True,error='计算进程中断；下次开始从已保存比较结果恢复')
                    else: message=pipe.recv()
                except (EOFError,OSError,ValueError): message=dict(done=True,error='计算进程中断；下次开始从已保存比较结果恢复')
                if message.get('consume'): self.consume_test(mode,message['consume'])
                if message.get('phase'): job['phase']=message['phase']
                if message.get('progress'):
                    message['updated']=now(); self.store.set('research-progress-'+mode,message)
                if message.get('done'):
                    if 'result' in message:
                        self.integrate_research(mode,message['result'],job)
                        if not job.get('cancel') or not job['cancel'].is_set(): self.store.set('researched-'+mode,job['week'])
                    else:
                        self.store.audit('策略检验未完成',dict(mode=mode,error=message['error']))
                        self.store.set('research_wait',message['error'])
                        reached=(self.store.get('research-progress-'+mode) or {}).get('progress',0)
                        self.store.set('research-progress-'+mode,dict(phase='error',progress=reached,message=message['error'],updated=now()))
                        # A transient failure resumes from the checkpoint after a pause.
                        self.store.set('research_retry_after',(self.clock()+timedelta(minutes=10)).isoformat())
                    self.end_research_job(job)
                    self.research_jobs.pop(mode,None)
                    break
            guard=job.get('guard')
            if guard:
                stats=guard.stats(); active=max(0,len(stats['pids'])-1) if job['phase']=='training' and job['workers']>1 else int(job['process'].is_alive())
                metrics=self.store.get('metrics',{}); self.store.set('metrics',metrics|dict(research_peak_memory_mb=max(metrics.get('research_peak_memory_mb',0),stats['peak_memory_mb']),observed_compute_tasks=max(metrics.get('observed_compute_tasks',0),active+sum((max(0,len(other['guard'].stats()['pids'])-1) if other['phase']=='training' and other['workers']>1 else int(other['process'].is_alive())) for other in self.research_jobs.values() if other is not job and other.get('guard')))))
    def integrate_research(self,mode,result,job):
        if job['baseline']!=self.store.strategy(mode)['id']:
            result=copy.deepcopy(result); result['gate']['passed']=False; result['gate']['reasons'].append('当前基准版本已变化，本次结果不能晋升')
        rid=uuid.uuid4().hex[:16]; path=self.root/'outputs'/('v3-research-'+rid+'.json'); path.write_text(dump(result),encoding='utf-8')
        with self.store.db() as db: db.execute('INSERT INTO experiments VALUES(?,?,?,?)',(rid,mode,now(),dump(dict(gate=result['gate'],windows=result['windows'],file=path.name,seconds=result['seconds'],key=job.get('key'),params=result.get('params'),checkpoint=job.get('checkpoint')))))
        if result['gate']['passed']:
            cid='v3-'+rid; evidence=result['gate']|dict(costs=result.get('costs',self.store.get('settings')['costs']),snapshot=result.get('snapshot'),engine_digest=result.get('engine_digest'))
            payload=dict(params=result['params'],created=now(),evidence=evidence,baseline=job['baseline'],control=self.store.strategy(mode)['payload']['params'],experiment=rid,key=job.get('key'))
            with self.store.db() as db:
                db.execute('INSERT INTO strategies VALUES(?,?,?,?)',(cid,mode,'challenger',dump(payload)))
                for prefix in ['shadow-','control-']: self.store.save_account(prefix+cid,self.store.empty_account(22000),db)
            self.store.audit('冻结后续模拟候选',dict(id=cid,mode=mode)); self.make_plans()
            self.store.set('research_wait','新候选已冻结，开始后续模拟观察；需至少20个共同交易日和10笔完整买卖')
        else:
            self.store.audit('候选不通过，保留原版',result['gate'])
            self.store.set('research_wait','本轮未通过，保留原版：'+'；'.join(result['gate']['reasons'][:4]))
        self.store.set('research_retry_after',None)
        self.store.set('research-progress-'+mode,dict(phase='done',progress=100,message='本轮检验完成',updated=now()))
    def review_forward(self,strategy):
        cid=strategy['id']; mode=strategy['mode']
        if strategy['payload'].get('evidence',{}).get('costs')!=self.store.get('settings')['costs']:
            with self.store.db() as db: db.execute("UPDATE strategies SET status='failed' WHERE id=? AND status='challenger'",(cid,))
            self.store.audit('费用模型变化，候选验证失效',dict(id=cid,mode=mode)); return
        result=forward_gate(self.store.account('shadow-'+cid),self.store.account('control-'+cid),LIMITS[mode])
        if result=='waiting': return
        with self.store.db() as db:
            if result=='passed':
                current=db.execute("SELECT payload FROM strategies WHERE id=? AND status='challenger'",(cid,)).fetchone()
                if not current: return
                payload=json.loads(current[0]); shadow=self.store.account('shadow-'+cid)
                payload['forward_evidence']=dict(passed=True,reviewed=now(),days=len(shadow['days']),closed=shadow['closed'],equity=rounded(self.store.equity_value(shadow)),account='shadow-'+cid,control='control-'+cid)
                db.execute('UPDATE strategies SET payload=? WHERE id=?',(dump(payload),cid))
                db.execute("UPDATE strategies SET status='retired' WHERE mode=? AND status='current'",(mode,)); db.execute("UPDATE strategies SET status='current' WHERE id=?",(cid,))
            else: db.execute("UPDATE strategies SET status='failed' WHERE id=?",(cid,))
        self.store.audit('自动晋升' if result=='passed' else '后续模拟失败，保留原版',dict(id=cid,mode=mode)); self.notify(cid+':review','策略比较完成','新策略已晋升' if result=='passed' else '新候选未通过，继续使用原版')
    def rollback(self,mode,cid):
        with self.store.lock,self.store.db() as db:
            r=db.execute("SELECT * FROM strategies WHERE id=? AND mode=? AND status='retired'",(cid,mode)).fetchone()
            if not r: raise ValueError('只可回退到同模式已使用的历史版本')
            payload=json.loads(r['payload']); evidence=payload.get('evidence')
            if evidence and evidence.get('costs')!=self.store.get('settings')['costs']:
                payload.setdefault('invalidated_evidence',[]).append(dict(evidence=evidence,forward=payload.get('forward_evidence'),reason='回退时费用模型已变化',time=now()))
                payload['evidence']=None; payload['forward_evidence']=None; db.execute('UPDATE strategies SET payload=? WHERE id=?',(dump(payload),cid))
            db.execute("UPDATE strategies SET status='retired' WHERE mode=? AND status='current'",(mode,)); db.execute("UPDATE strategies SET status='current' WHERE id=?",(cid,))
        self.store.audit('手动回退',dict(mode=mode,id=cid))
