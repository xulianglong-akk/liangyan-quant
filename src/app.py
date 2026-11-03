import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(Path('E:/AI-Tools/quant-assistant/python-packages')))
import csv
import hashlib
import io
import json
import math
import os
import re
import socket
import sqlite3
import threading
import time
import traceback
import uuid
import webbrowser
from contextlib import contextmanager, closing
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from engine import MODES, DEFAULT_COSTS, features, plan, backtest, corporate_actions, fee
from daily_guide import daily_decision, describe_error

TZ=timezone(timedelta(hours=8))
DATA_ROOT=Path(os.environ.get('QUANT_DATA_HOME','D:/量化数据'))
DB=Path(os.environ.get('QUANT_DB',str(DATA_ROOT/'data'/'research.sqlite3')))
DB.parent.mkdir(parents=True,exist_ok=True)
socket.setdefaulttimeout(25)
DEFAULT_POOL={'sh.600036':'招商银行','sh.600519':'贵州茅台','sh.601318':'中国平安','sh.600900':'长江电力','sh.601166':'兴业银行','sh.600030':'中信证券','sz.000333':'美的集团','sz.000651':'格力电器','sz.002594':'比亚迪','sz.002415':'海康威视','sh.600276':'恒瑞医药','sz.000858':'五粮液'}
JOB_LOCK=threading.Lock(); WRITE_LOCK=threading.Lock(); CANCEL=threading.Event(); JOB=dict(status='idle',progress=0,message='等待任务',kind='',started=None,ended=None,error=None)

def now(): return datetime.now(TZ).isoformat(timespec='seconds')
def completed_day():
    clock=datetime.now(TZ)
    cutoff=(clock.date()-timedelta(days=1)).isoformat() if clock.hour<18 else clock.date().isoformat()
    with connect() as db: return db.execute('SELECT max(date) FROM calendar WHERE open=1 AND date<=?',(cutoff,)).fetchone()[0]
@contextmanager
def connect():
    db=sqlite3.connect(DB,timeout=30); db.row_factory=sqlite3.Row; db.execute('PRAGMA journal_mode=WAL')
    try:
        with db: yield db
    finally: db.close()
def init():
    with connect() as db:
        db.executescript('''CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE IF NOT EXISTS bars(code TEXT,date TEXT,payload TEXT,fetched TEXT,PRIMARY KEY(code,date));
        CREATE TABLE IF NOT EXISTS calendar(date TEXT PRIMARY KEY,open INTEGER);
        CREATE TABLE IF NOT EXISTS logs(id INTEGER PRIMARY KEY AUTOINCREMENT,time TEXT,level TEXT,message TEXT);
        CREATE TABLE IF NOT EXISTS recommendations(id TEXT PRIMARY KEY,created TEXT,mode TEXT,payload TEXT);
        CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,created TEXT,kind TEXT,payload TEXT);
        CREATE TABLE IF NOT EXISTS journal(id TEXT PRIMARY KEY,created TEXT,account TEXT,code TEXT,side TEXT,date TEXT,price REAL,quantity INTEGER,fees REAL,plan_id TEXT);
        CREATE TABLE IF NOT EXISTS versions(id TEXT PRIMARY KEY,created TEXT,mode TEXT,params TEXT,status TEXT,evidence TEXT);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,time TEXT,action TEXT,payload TEXT);''')
        for mode,p in MODES.items(): db.execute('INSERT OR IGNORE INTO versions VALUES(?,?,?,?,?,?)',('v1-'+mode,now(),mode,json.dumps(p),'current','初始解释性规则；未声称已验证有效'))
    if get('settings') is None: put('settings',dict(mode='steady',sim_capital=100000,real_capital=None,costs=DEFAULT_COSTS,auto_update=True,auto_promote=False,frozen=False,start_date='2024-01-01'))
    if get('pool') is None: put('pool',DEFAULT_POOL)
    previous=get('last_job')
    if previous:
        JOB.update(previous)
        if JOB['status']=='running': JOB.update(status='cancelled',message='上次任务因服务停止中断，缓存保留；可重新执行',ended=now())

def get(key):
    with connect() as db: row=db.execute('SELECT value FROM kv WHERE key=?',(key,)).fetchone()
    return json.loads(row[0]) if row else None
def put(key,value):
    with connect() as db: db.execute('INSERT OR REPLACE INTO kv VALUES(?,?)',(key,json.dumps(value,ensure_ascii=False)))
def log(message,level='info'):
    with connect() as db: db.execute('INSERT INTO logs(time,level,message) VALUES(?,?,?)',(now(),level,message))
def progress(value,message):
    JOB.update(progress=value,message=message); log(message)
def load_bars(code):
    with connect() as db: rows=db.execute('SELECT payload FROM bars WHERE code=? ORDER BY date',(code,)).fetchall()
    return [json.loads(r[0]) for r in rows]
def params(mode):
    with connect() as db: r=db.execute("SELECT * FROM versions WHERE mode=? AND status='current'",(mode,)).fetchone()
    return r['id'],json.loads(r['params'])
def require_date(s):
    if not isinstance(s,str): raise ValueError('日期必填')
    datetime.strptime(s,'%Y-%m-%d'); return s
def require_code(code):
    if not re.fullmatch(r'(sh\.(600|601|603|605)\d{3}|sz\.(000|001|002|003)\d{3})',code or ''): raise ValueError('首版仅支持沪深普通主板股票，例如 sh.600036；暂不支持创业板、科创板、北交所')
    return code
def bs_rows(result):
    if result.error_code!='0': raise RuntimeError(f'BaoStock {result.error_code}: {result.error_msg}')
    out=[]
    while result.next():
        if CANCEL.is_set(): raise InterruptedError('任务已取消')
        out.append(dict(zip(result.fields,result.get_row_data())))
    return out

def update_data():
    import baostock as bs
    settings=get('settings'); pool=get('pool'); today=datetime.now(TZ).date().isoformat()
    progress(2,'连接 BaoStock（历史日线，非实时行情）')
    login=bs.login()
    if login.error_code!='0': raise RuntimeError('BaoStock 登录失败：'+login.error_msg)
    try:
        calendar_end=(datetime.now(TZ).date()+timedelta(days=45)).isoformat()
        cal=bs_rows(bs.query_trade_dates(start_date=settings['start_date'],end_date=calendar_end))
        with connect() as db:
            for r in cal: db.execute('INSERT OR REPLACE INTO calendar VALUES(?,?)',(r['calendar_date'],int(r['is_trading_day'])))
        names=bs_rows(bs.query_stock_basic())
        lookup={r['code']:r for r in names}
        for c in pool:
            if c not in lookup or lookup[c].get('status')!='1': raise ValueError(f'{c} 未找到正常上市信息，缓存保留，请检查观察池')
            pool[c]=lookup[c]['code_name']
        put('pool',pool); put('stock_metadata',{c:lookup[c] for c in pool})
        codes=list(pool)+['sh.000300']; failures=[]
        for idx,code in enumerate(codes):
            if CANCEL.is_set(): raise InterruptedError('任务已取消；已完成股票缓存保留')
            old=load_bars(code); start=max(settings['start_date'],(datetime.strptime(old[-1]['date'],'%Y-%m-%d')-timedelta(days=10)).date().isoformat()) if old else settings['start_date']
            progress(5+int(85*idx/len(codes)),f'更新 {pool.get(code,"沪深300")} · {idx+1}/{len(codes)}')
            err=None
            for attempt in range(2):
                try:
                    fields='date,code,open,high,low,close,preclose,volume,amount,tradestatus,pctChg,isST' if code!='sh.000300' else 'date,code,open,high,low,close,preclose,volume,amount,pctChg'
                    raw=bs_rows(bs.query_history_k_data_plus(code,fields,start_date=start,end_date=today,frequency='d',adjustflag='3'))
                    validated=[]; last=None
                    for r in raw:
                        if r['date']==today and datetime.now(TZ).hour<18: continue
                        if last and r['date']<=last: raise ValueError('日期乱序或重复')
                        last=r['date']; b={k:r.get(k,'0') for k in ['date','code','tradestatus','isST']}
                        if code=='sh.000300': b.update(tradestatus='1',isST='0')
                        for k in ['open','high','low','close','preclose','volume','amount','pctChg']: b[k]=float(r.get(k) or 0)
                        if not all(math.isfinite(b[k]) for k in ['open','high','low','close','volume','amount']): raise ValueError('非有限价格')
                        if b['close']<=0 or b['preclose']<=0 or b['volume']<0 or b['amount']<0: raise ValueError('无效价格或成交量')
                        if b['tradestatus']=='1' and not (b['low']<=min(b['open'],b['close'])<=max(b['open'],b['close'])<=b['high']): raise ValueError('OHLC 不一致')
                        validated.append(b)
                    if not old and not validated: raise ValueError('数据源返回空日线')
                    combined={b['date']:b for b in old}
                    combined.update({b['date']:b for b in validated})
                    end_date=max(combined) if combined else start
                    ipo=lookup.get(code,{}).get('ipoDate',settings['start_date'])
                    first_date=max(min(combined) if combined else start,ipo or settings['start_date'])
                    expected_dates={r['calendar_date'] for r in cal if r['is_trading_day']=='1' and first_date<=r['calendar_date']<=end_date}
                    missing=expected_dates-set(combined)
                    if missing: raise ValueError('交易日线缺失，保留原缓存：'+','.join(sorted(missing)[:10]))
                    with connect() as db:
                        for b in validated: db.execute('INSERT OR REPLACE INTO bars VALUES(?,?,?,?)',(code,b['date'],json.dumps(b),now()))
                    err=None; break
                except InterruptedError: raise
                except Exception as e:
                    err=str(e); log(f'{code} 第{attempt+1}次获取失败：{err}','error')
                    if attempt==0 and CANCEL.wait(1): raise InterruptedError('已取消')
            if err: failures.append(code+': '+err)
        put('last_update',dict(time=now(),failed=failures,source='BaoStock',type='历史日线快照'))
        if failures: raise RuntimeError('部分更新失败，已保留缓存：'+'；'.join(failures))
        progress(94,'生成并保存两种模式的不可改写交易计划')
        make_recommendations(); progress(100,'行情更新与计划生成完成')
    finally: bs.logout()

def dataset():
    values={c:load_bars(c) for c in get('pool')}
    return {c:bs for c,bs in values.items() if bs}
def make_recommendations():
    ds=dataset(); settings=get('settings'); pool=get('pool')
    if not ds: raise ValueError('尚无真实日线，请先更新数据')
    expected=completed_day()
    latest=max(bs[-1]['date'] for bs in ds.values())
    capital=settings['real_capital'] if settings.get('real_capital') else settings['sim_capital']
    account='real' if settings.get('real_capital') else 'sim'
    held=holdings(account)
    with connect() as db: records=[dict(r) for r in db.execute('SELECT * FROM journal WHERE account=?',(account,))]
    cash=max(0,capital+sum((r['price']*r['quantity'] if r['side']=='sell' else -r['price']*r['quantity'])-r['fees'] for r in records))
    held_value=sum(h['quantity']*(h['price'] if h['price'] is not None else h['avg']) for h in held)
    equity=cash+held_value
    held_codes={h['code'] for h in held}
    # Date validity is explicit; plans made from old closes are research observations only.
    for mode in MODES:
        version,p=params(mode); plans=[]
        for c,bs in ds.items():
            if bs[-1]['date']!=latest: continue
            if c in held_codes: continue
            value=plan(bs,p,equity,version,c,pool[c])
            if value: value['status']='待下一交易日开盘核验'; plans.append(value)
        plans=sorted(plans,key=lambda x:x['score'],reverse=True)[:max(0,p['slots']-len(held))]
        remaining=min(cash,max(0,equity*p['exposure']-held_value))
        for value in plans:
            price=value['buy_high']; qty=min(value['quantity'],int(remaining/price/100)*100)
            while qty>0 and qty*price+fee(qty*price,'buy',latest,settings['costs'])>min(remaining,value['budget']): qty-=100
            cost=qty*price; charge=fee(cost,'buy',latest,settings['costs']) if qty else 0
            value.update(quantity=qty,estimated_buy_fee=round(charge,2),estimated_total=round(cost+charge,2),capital_kind=account,quantity_note='资金分配后不足100股，暂不买入' if not qty else '数量已考虑当前账本持仓、可用资金和买入手续费')
            remaining-=cost+charge
        payload=dict(asof=latest,created=now(),expected=expected,mode=mode,version=version,plans=plans,universe=list(pool),sim_capital=settings['sim_capital'],capital=capital,capital_kind=account,cash=round(cash,2),held_value=round(held_value,2),params=p)
        fingerprint=hashlib.sha256(json.dumps(payload|{'created':None},sort_keys=True).encode()).hexdigest()[:24]
        with connect() as db: db.execute('INSERT OR IGNORE INTO recommendations VALUES(?,?,?,?)',(fingerprint,payload['created'],mode,json.dumps(payload,ensure_ascii=False)))
    return True

def save_run(kind,value,manifest):
    rid=uuid.uuid4().hex[:12]; stamp=now(); value.update(id=rid,created=stamp,manifest=manifest)
    path=DATA_ROOT/'outputs'/f'{kind}-{rid}.json'; path.parent.mkdir(parents=True,exist_ok=True); path.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    with connect() as db: db.execute('INSERT INTO runs VALUES(?,?,?,?)',(rid,stamp,kind,json.dumps(value,ensure_ascii=False)))
    return value
def run_backtest(body):
    mode=body.get('mode',get('settings')['mode']); version,p=params(mode)
    start=require_date(body['start']); end=require_date(body['end']); ds=dataset(); settings=get('settings')
    if len(ds)!=len(get('pool')): raise ValueError('观察池数据不完整，请先更新全部股票')
    if start<'2023-01-01': raise ValueError('首版费用模型仅支持2023年及以后')
    if start<max(bs[0]['date'] for bs in ds.values()) or end>min(bs[-1]['date'] for bs in ds.values()): raise ValueError('请求区间超出观察池共同缓存范围，请更新数据或调整区间')
    snap=json.dumps(ds,sort_keys=True); digest=hashlib.sha256(snap.encode()).hexdigest()
    progress(15,'计算组合回测：昨日信号 → 次日开盘')
    r=backtest(ds,get('pool'),p,start,end,settings['sim_capital'],settings['costs'],CANCEL.is_set,load_bars('sh.000300'))
    manifest=dict(engine='1.0.0',data_sha256=digest,version=version,params=p,costs=settings['costs'],capital=settings['sim_capital'],seed=0,source='BaoStock/raw',universe=get('pool'),benchmark=load_bars('sh.000300'))
    snapshot=DATA_ROOT/'outputs'/f'data-{digest[:16]}.json'
    snapshot.parent.mkdir(parents=True,exist_ok=True)
    if not snapshot.exists(): snapshot.write_text(snap,encoding='utf-8')
    manifest['snapshot']=snapshot.name; save_run('backtest',r,manifest); progress(100,'回测完成，数据快照与参数已保存')

def research():
    settings=get('settings'); mode=settings['mode']; version,p=params(mode); ds=dataset(); pool=get('pool')
    if settings['frozen']: raise ValueError('策略已冻结，请先解除冻结')
    dates=sorted(set.intersection(*[{b['date'] for b in bs} for bs in ds.values()])) if ds else []
    last_action=max([d for bs in ds.values() for d in corporate_actions(bs)]+['1900-01-01'])
    dates=[d for d in dates if d>last_action]
    if len(dates)<250: raise ValueError(f'共同的无公司行动区间仅 {len(dates)} 个交易日；受约束迭代需至少250日，本版不会用不完整权益数据强行晋升')
    split1=int(len(dates)*.6); split2=int(len(dates)*.8); test_start=dates[split2]
    consumed=get('test_consumed_'+mode)
    if consumed and test_start<=consumed: raise ValueError('独立测试区间与已用区间重叠。须等待至少新的独立时间区间；不能重复使用同一测试段调参')
    windows=[(dates[0],dates[split1-1]),(dates[split1],dates[split2-1]),(test_start,dates[-1])]
    trials=[]
    for idx,delta in enumerate([-0.01,0,0.01]):
        if CANCEL.is_set(): raise InterruptedError('研究已取消')
        candidate=p|{'momentum':max(0,p['momentum']+delta)}; progress(10+idx*20,f'训练候选 {idx+1}/3（仅阈值网格）')
        r=backtest(ds,pool,candidate,*windows[0],settings['sim_capital'],settings['costs'],CANCEL.is_set,load_bars('sh.000300'))
        trials.append((r['metrics']['total_return']-r['metrics']['max_drawdown'],candidate,r['metrics']))
    _,candidate,training=max(trials,key=lambda x:x[0]); progress(75,'冻结训练胜者，执行验证与一次独立测试')
    # Reserve holdout before reading it, even if interrupted; prevent accidental reuse.
    put('test_consumed_'+mode,dates[-1])
    comparisons=[]
    mid=(split1+split2)//2
    for start,end in [windows[1],(dates[split1],dates[mid-1]),(dates[mid],dates[split2-1]),windows[2]]:
        a=backtest(ds,pool,candidate,start,end,settings['sim_capital'],settings['costs'],CANCEL.is_set,load_bars('sh.000300'))['metrics']
        b=backtest(ds,pool,p,start,end,settings['sim_capital'],settings['costs'],CANCEL.is_set,load_bars('sh.000300'))['metrics']
        comparisons.append(dict(start=start,end=end,candidate=a,current=b))
    passed=all(x['candidate']['trades']>=8 and x['candidate']['max_drawdown']<=.2 and x['candidate']['total_return']>x['current']['total_return'] and x['candidate']['total_return']>(x['candidate']['benchmark_return'] or 0) for x in comparisons)
    cid='candidate-'+uuid.uuid4().hex[:8]
    evidence=dict(windows=windows,training=training,comparisons=comparisons,passed=passed,gate='每个验证/测试窗口至少8笔已平仓交易、回撤≤20%、收益超过当前与价格指数基准',trials=3,selection_bias='固定现存观察池；探索性研究，未修正多重检验',promotion='不达门槛则保留原版')
    with connect() as db: db.execute('INSERT INTO versions VALUES(?,?,?,?,?,?)',(cid,now(),mode,json.dumps(candidate),'candidate',json.dumps(evidence,ensure_ascii=False)))
    result=save_run('research',evidence,dict(current=version,candidate=cid,params=candidate,seed=0))
    if passed and settings['auto_promote']: promote(cid)
    progress(100,'研究结束：'+('满足预设门槛' if passed else '未达门槛，保留当前版本'))

def promote(cid,rollback=False):
    settings=get('settings')
    if JOB['status']=='running' and not (JOB['kind']=='research' and settings['auto_promote']): raise ValueError('后台任务运行中，暂不能变更策略版本')
    if settings['frozen']: raise ValueError('策略已冻结')
    with connect() as db:
        row=db.execute('SELECT * FROM versions WHERE id=?',(cid,)).fetchone()
        if not row: raise ValueError('版本不存在')
        if rollback and row['status']!='archived': raise ValueError('只能回滚至已使用的归档版本')
        if not rollback:
            try: approved=json.loads(row['evidence']).get('passed',False)
            except (ValueError,AttributeError): approved=False
            if row['status']!='candidate' or not approved: raise ValueError('候选未通过门槛，禁止晋升')
        db.execute("UPDATE versions SET status='archived' WHERE mode=? AND status='current'",(row['mode'],))
        db.execute("UPDATE versions SET status='current' WHERE id=?",(cid,))
        db.execute('INSERT INTO events(time,action,payload) VALUES(?,?,?)',(now(),'rollback' if rollback else 'promote',cid))
    make_recommendations()

def start_job(kind,body=None):
    with JOB_LOCK:
        if JOB['status']=='running': raise ValueError('已有后台任务在运行，并发上限为1')
        CANCEL.clear(); JOB.update(status='running',progress=0,message='准备任务',kind=kind,started=now(),ended=None,error=None,seconds=None)
        put('last_job',JOB.copy())
    def worker():
        began=time.perf_counter()
        terminal_status='done'; terminal_error=None; terminal_message=None
        try:
            if kind=='update': update_data()
            elif kind=='backtest': run_backtest(body)
            elif kind=='research': research()
            elif kind=='plans': make_recommendations(); progress(100,'交易计划已生成')
            else: raise ValueError('未知任务')
        except InterruptedError as e: terminal_status='cancelled'; terminal_message=str(e); log(str(e),'warning')
        except Exception as e: terminal_status='failed'; terminal_error=str(e); terminal_message='任务失败，原缓存保留'; log(str(e),'error'); traceback.print_exc()
        finally:
            with JOB_LOCK:
                finished=JOB.copy()
                finished.update(status=terminal_status,error=terminal_error,ended=now(),seconds=round(time.perf_counter()-began,3))
                if terminal_message: finished['message']=terminal_message
                put('last_job',finished)
                JOB.update(finished)
    threading.Thread(target=worker,daemon=True).start(); return JOB.copy()

def holdings(account):
    with connect() as db: rows=[dict(r) for r in db.execute('SELECT * FROM journal WHERE account=? ORDER BY date,created,rowid',(account,))]
    pos={}
    for r in rows:
        c=r['code']; item=pos.setdefault(c,dict(code=c,name=get('pool').get(c,c),quantity=0,cost=0,first_date=r['date']))
        if 'plan_id' not in item: item['plan_id']=r['plan_id']
        if r['side']=='buy': item['quantity']+=r['quantity']; item['cost']+=r['price']*r['quantity']+r['fees']
        else:
            avg=item['cost']/item['quantity'] if item['quantity'] else 0; item['quantity']-=r['quantity']; item['cost']-=avg*r['quantity']
        if item['quantity']==0: pos.pop(c,None)
    out=[]
    _,p=params(get('settings')['mode'])
    for c,v in pos.items():
        bs=load_bars(c); avg=v['cost']/v['quantity']; v['avg']=avg; v['price']=bs[-1]['close'] if bs else None; v['asof']=bs[-1]['date'] if bs else None
        v['pnl']=(v['price']-avg)*v['quantity'] if bs else None; v['stop']=round(avg*(1-p['stop']),2); v['target']=round(avg*(1+p['target']),2)
        trail=p['trail']; max_hold=p['hold']
        if v['plan_id']:
            with connect() as db: rec=db.execute('SELECT payload FROM recommendations WHERE id=?',(v['plan_id'],)).fetchone()
            original=next((pl for pl in json.loads(rec[0])['plans'] if pl['code']==c),None) if rec else None
            if original: v.update(stop=original['stop'],target=original['target']); trail=original['trail']; max_hold=original['hold']
        v['alert']='核对原始成交计划' if not bs else ('止损条件触发，核对最新行情后手动处理' if v['price']<=v['stop'] else '止盈条件触发，核对最新行情后手动处理' if v['price']>=v['target'] else '价格未触及当前模式参考阈值')
        since=[b for b in bs if b['date']>=v['first_date']]
        if since:
            v['holding_days']=len(since); v['trailing_stop']=round(max(b['close'] for b in since)*(1-trail),2)
            if v['holding_days']>=max_hold: v['alert']='最长持有期已达，核对原始计划后手动处理'
            if v['price']<=v['trailing_stop']: v['alert']='跟踪止损条件触发，核对最新行情后手动处理'
        if bs and any(d>v['first_date'] for d in corporate_actions(bs)): v['alert']='持有期间存在公司行动，请核对券商股数、分红及成本；本账本未自动入账'
        out.append(v)
    return out

def add_trade(body):
    account=body.get('account','sim'); code=require_code(body.get('code')); side=body.get('side'); date=require_date(body.get('date'))
    if account not in ['sim','real'] or side not in ['buy','sell']: raise ValueError('账户或方向无效')
    settings=get('settings')
    if account=='real' and not settings['real_capital']: raise ValueError('请先填写真实资金设置，再录入真实成交')
    price=float(body['price']); qty=int(body['quantity']); fees=float(body.get('fees',0))
    if float(body['quantity'])!=qty: raise ValueError('股数必须为整数')
    if not math.isfinite(price) or price<=0 or qty<=0 or not math.isfinite(fees) or fees<0: raise ValueError('价格、股数、费用必须有效')
    if date>datetime.now(TZ).date().isoformat(): raise ValueError('不能录入未来成交')
    with WRITE_LOCK,connect() as db:
        cal=db.execute('SELECT open FROM calendar WHERE date=?',(date,)).fetchone()
        if not cal or not cal[0]: raise ValueError('该日期不在已验证交易日历内，请先更新数据')
        previous=[dict(r) for r in db.execute('SELECT * FROM journal WHERE account=? ORDER BY date,created,rowid',(account,))]
        if previous and date<previous[-1]['date']: raise ValueError('首版请按成交日期顺序录入，避免历史成交改变后续可卖股数')
        available=sum((r['quantity'] if r['side']=='buy' else -r['quantity']) for r in previous if r['code']==code)
        if side=='buy' and qty%100: raise ValueError('普通主板买入须为100股整数倍')
        if side=='sell':
            sellable=sum((r['quantity'] if r['side']=='buy' else -r['quantity']) for r in previous if r['code']==code and (r['side']=='sell' or r['date']<date))
            if qty>sellable: raise ValueError('可卖股数不足或违反T+1')
            if qty%100 and qty!=available: raise ValueError('零股须一次性全部卖出')
        capital=settings['sim_capital'] if account=='sim' else settings['real_capital']
        cash=capital+sum((r['price']*r['quantity'] if r['side']=='sell' else -r['price']*r['quantity'])-r['fees'] for r in previous)
        if side=='buy' and price*qty+fees>cash: raise ValueError('账本可用资金不足')
        rid=uuid.uuid4().hex[:12]
        db.execute('INSERT INTO journal VALUES(?,?,?,?,?,?,?,?,?,?)',(rid,now(),account,code,side,date,price,qty,fees,body.get('plan_id','')))
    return dict(id=rid)

def state():
    pool=get('pool'); settings=get('settings'); stocks=[]
    for c,name in pool.items():
        bs=load_bars(c); _,p=params(settings['mode']); f=features(bs,p) if bs else None
        stocks.append(dict(code=c,name=name,bars=len(bs),last=bs[-1] if bs else None,features=f,actions=corporate_actions(bs)))
    with connect() as db:
        recs=[dict(id=r['id'],**json.loads(r['payload'])) for r in db.execute('SELECT * FROM recommendations ORDER BY rowid DESC LIMIT 60')]
        runs=[dict(id=r['id'],created=r['created'],kind=r['kind'],result=json.loads(r['payload'])) for r in db.execute('SELECT * FROM runs ORDER BY rowid DESC LIMIT 12')]
        versions=[dict(r) for r in db.execute('SELECT * FROM versions ORDER BY rowid DESC')]
        logs=[dict(r) for r in db.execute('SELECT * FROM logs ORDER BY id DESC LIMIT 20')]
        journal=[dict(r) for r in db.execute('SELECT * FROM journal ORDER BY date DESC,created DESC,rowid DESC LIMIT 100')]
        expected=completed_day()
        fetched=db.execute('SELECT max(fetched) FROM bars').fetchone()[0]
    for rec in recs:
        with connect() as db: next_date=db.execute('SELECT min(date) FROM calendar WHERE open=1 AND date>?',(rec['asof'],)).fetchone()[0]
        clock=datetime.now(TZ)
        rec['entry_date']=next_date
        rec['validity']='待下一交易日开盘核验' if not next_date or clock.date().isoformat()<next_date or (clock.date().isoformat()==next_date and clock.strftime('%H:%M')<'09:30') else '有效开盘已过，仅供复盘'
        rec['stale']=bool(expected and rec['asof']<expected)
    tracking=[]
    for rec in recs:
        for pl in rec['plans']:
            bs=load_bars(pl['code']); future=[b for b in bs if b['date']>rec['asof']]
            action=any(d>rec['asof'] for d in corporate_actions(bs))
            tracking.append(dict(rec_id=rec['id'],date=rec['asof'],mode=rec['mode'],version=rec['version'],code=pl['code'],name=pl['name'],close=pl['features']['close'],after_days=len(future),return_pct=(future[-1]['close']/pl['features']['close']-1) if future and not action else None,note='公司行动跨期，暂停价格收益统计' if action else '信号后价格变化；不是策略成交收益',asof=future[-1]['date'] if future else None))
    rec=next((r for r in recs if r['mode']==settings['mode']),None)
    decision=daily_decision(settings,stocks,rec,expected,JOB)
    return dict(time=now(),settings=settings,pool=pool,stocks=stocks,job=JOB.copy(),recommendations=recs,runs=runs,versions=versions,logs=logs,journal=journal,holdings=dict(sim=holdings('sim'),real=holdings('real')),tracking=tracking,expected=expected,fetched=fetched,last_update=get('last_update'),modes=MODES,decision=decision,job_explanation=describe_error(JOB.get('error')) if JOB.get('error') else None)

def backup():
    name='backup-'+datetime.now(TZ).strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:6]+'.sqlite3'; path=DATA_ROOT/'backups'/name
    path.parent.mkdir(parents=True,exist_ok=True)
    with connect() as src,closing(sqlite3.connect(path)) as dst: src.backup(dst)
    return dict(file=name)

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def respond(self,value,status=200):
        raw=json.dumps(value,ensure_ascii=False,allow_nan=False).encode(); self.send_response(status); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.end_headers(); self.wfile.write(raw)
    def local(self):
        host=self.headers.get('Host','')
        return host in [f'127.0.0.1:{PORT}',f'localhost:{PORT}']
    def do_GET(self):
        if not self.local(): return self.respond(dict(error='仅允许本机访问'),403)
        parsed=urlparse(self.path)
        try:
            if parsed.path=='/api/state': return self.respond(state())
            if parsed.path=='/api/bars': return self.respond(load_bars(parse_qs(parsed.query).get('code',[''])[0]))
            if parsed.path=='/api/export':
                with connect() as db: rows=[dict(r) for r in db.execute('SELECT * FROM journal ORDER BY date,created')]
                out=io.StringIO(); w=csv.DictWriter(out,fieldnames=['id','created','account','code','side','date','price','quantity','fees','plan_id']); w.writeheader(); w.writerows(rows)
                raw=('\ufeff'+out.getvalue()).encode(); self.send_response(200); self.send_header('Content-Type','text/csv; charset=utf-8'); self.send_header('Content-Disposition','attachment; filename=trades.csv'); self.end_headers(); self.wfile.write(raw); return
            path=(ROOT/'web'/('index.html' if parsed.path=='/' else parsed.path.lstrip('/'))).resolve()
            if not path.is_relative_to(ROOT/'web') or not path.is_file(): return self.respond(dict(error='文件不存在'),404)
            mime={'.html':'text/html','.css':'text/css','.js':'text/javascript','.svg':'image/svg+xml'}.get(path.suffix,'application/octet-stream'); self.send_response(200); self.send_header('Content-Type',mime+'; charset=utf-8'); self.send_header('Cache-Control','no-cache'); self.end_headers(); self.wfile.write(path.read_bytes())
        except Exception as e: self.respond(dict(error=str(e)),400)
    def do_POST(self):
        if not self.local(): return self.respond(dict(error='仅允许本机访问'),403)
        origin=self.headers.get('Origin')
        if origin and origin not in [f'http://127.0.0.1:{PORT}',f'http://localhost:{PORT}']: return self.respond(dict(error='拒绝跨站写入'),403)
        if self.headers.get('X-Quant-Local')!='1': return self.respond(dict(error='缺少本地请求标记'),403)
        try:
            size=int(self.headers.get('Content-Length',0))
            if size>100000: raise ValueError('请求过大')
            body=json.loads(self.rfile.read(size) or b'{}'); path=urlparse(self.path).path
            if path=='/api/jobs': value=start_job(body['kind'],body)
            elif path=='/api/cancel': CANCEL.set(); value=dict(message='已请求取消，当前网络请求结束后生效（超时25秒）')
            elif path=='/api/settings':
                if JOB['status']=='running': raise ValueError('请等待或取消后台任务后修改设置')
                old=get('settings'); backup()
                new=old|body
                if new['mode'] not in MODES: raise ValueError('模式无效')
                for k in ['sim_capital','real_capital']:
                    if k=='real_capital' and new[k] is None: continue
                    if not isinstance(new[k],(int,float)) or not math.isfinite(new[k]) or new[k]<=0 or new[k]>1e9: raise ValueError('资金须为有效正数，且不超过10亿元')
                for k in ['auto_update','auto_promote','frozen']:
                    if not isinstance(new[k],bool): raise ValueError('开关须为布尔值')
                require_date(new['start_date'])
                if new['start_date']<'2005-01-01' or new['start_date']>datetime.now(TZ).date().isoformat(): raise ValueError('历史起点须在2005年至今天之间')
                if set(new['costs'])!=set(DEFAULT_COSTS): raise ValueError('费用字段不完整')
                for k,v in new['costs'].items():
                    cap=100 if k=='minimum' else .05
                    if not isinstance(v,(int,float)) or not math.isfinite(v) or v<0 or v>cap: raise ValueError('费用或流动性参数超出范围')
                if new['costs']['participation']<=0: raise ValueError('成交占比必须为正数')
                put('settings',new)
                if dataset(): make_recommendations()
                value=new
            elif path=='/api/pool':
                if JOB['status']=='running': raise ValueError('请等待后台任务完成后修改观察池')
                codes=list(dict.fromkeys(body['codes']))
                if not 1<=len(codes)<=30: raise ValueError('观察池限1至30只')
                for c in codes: require_code(c)
                backup(); old=get('pool'); put('pool',{c:old.get(c,c) for c in codes}); value=get('pool')
            elif path=='/api/trades':
                value=add_trade(body)
                if JOB['status']!='running' and dataset(): make_recommendations()
            elif path=='/api/backup': value=backup()
            elif path=='/api/promote': promote(body['id']); value=dict(ok=True)
            elif path=='/api/rollback': promote(body['id'],True); value=dict(ok=True)
            elif path=='/api/shutdown':
                CANCEL.set(); value=dict(ok=True,message='正在停止本地服务')
                threading.Thread(target=self.server.shutdown,daemon=True).start()
            else: return self.respond(dict(error='未知接口'),404)
            return self.respond(value)
        except Exception as e:
            explanation=describe_error(str(e))
            return self.respond(dict(error=str(e),explanation=explanation if explanation['title']!='这一步没有完成。' else None),400)

def scheduler():
    while True:
        time.sleep(30)
        settings=get('settings'); clock=datetime.now(TZ)
        if not settings['auto_update'] or JOB['status']=='running': continue
        attempt=get('scheduled_attempt') or {}
        with connect() as db: day=db.execute('SELECT open FROM calendar WHERE date=?',(clock.date().isoformat(),)).fetchone()
        ds=dataset(); stale=not ds or len(ds)!=len(get('pool')) or min(bs[-1]['date'] for bs in ds.values())<clock.date().isoformat()
        eligible=attempt.get('day')!=clock.date().isoformat() or (attempt.get('count',0)<3 and time.time()-attempt.get('timestamp',0)>=3600)
        if day and day[0] and clock.hour>=18 and stale and eligible:
            count=attempt.get('count',0)+1 if attempt.get('day')==clock.date().isoformat() else 1
            put('scheduled_attempt',dict(day=clock.date().isoformat(),count=count,timestamp=time.time())); start_job('update')

PORT=int(os.environ.get('QUANT_PORT','8765'))
if __name__=='__main__':
    init()
    try: server=ThreadingHTTPServer(('127.0.0.1',PORT),Handler)
    except OSError: print(f'端口{PORT}已占用，请检查软件是否已启动'); sys.exit(1)
    log('本地服务启动；日线快照，不提供授权实时行情')
    threading.Thread(target=scheduler,daemon=True).start()
    last=get('last_update')
    ds=dataset(); expected=completed_day()
    incomplete=not ds or len(ds)!=len(get('pool')) or (expected and min(bs[-1]['date'] for bs in ds.values())<expected)
    if get('settings')['auto_update'] and (incomplete or not last or str(last.get('time',''))[:10]!=datetime.now(TZ).date().isoformat()): start_job('update')
    print(f'量研工作台 http://127.0.0.1:{PORT} · Ctrl+C 停止',flush=True)
    if '--open' in sys.argv: webbrowser.open(f'http://127.0.0.1:{PORT}')
    try: server.serve_forever()
    except KeyboardInterrupt: CANCEL.set()
    finally:
        CANCEL.set(); server.server_close(); print('服务已停止')
