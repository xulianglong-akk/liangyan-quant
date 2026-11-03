"""Public source adapters. BaoStock runs in an interruptible, deadline-limited process."""
import json
import math
import multiprocessing as mp
import re
import socket
import sys
import threading
import time
from datetime import datetime, timedelta
from urllib.request import Request, build_opener, ProxyHandler
from rules_v3 import TZ, parse_action

FIELDS='date,code,open,high,low,close,preclose,volume,amount,tradestatus,pctChg,isST'

def rows(result):
    if result is None: raise RuntimeError('BaoStock 没有返回数据对象')
    if result.error_code!='0': raise RuntimeError('BaoStock '+result.error_code+': '+result.error_msg)
    out=[]
    while result.next():
        value=result.get_row_data()
        if len(value)!=len(result.fields): raise RuntimeError('BaoStock 返回字段数量异常')
        out.append(dict(zip(result.fields,value)))
    # A later pagination request can fail after a successful first page.
    if result.error_code!='0': raise RuntimeError('BaoStock 分页失败 '+result.error_code+': '+result.error_msg)
    return out

def _bs_worker(pipe,kind,args):
    if not getattr(sys,'frozen',False) and not __import__('importlib.util').util.find_spec('baostock'):
        sys.path.insert(0,'E:/AI-Tools/quant-assistant/python-packages')
    socket.setdefaulttimeout(30)
    import baostock as bs
    import baostock.common.context as bs_context
    from baostock.security.sectorinfo import query_terminated_stocks
    import baostock.util.socketutil as bs_socket
    raw_send=bs_socket.send_msg
    def checked_send(message):
        value=raw_send(message)
        # SDK pagination otherwise returns a truncated "successful" result on a
        # timeout / missing socket response. Only this worker is adapted.
        if value is None or not value.strip(): raise RuntimeError('BaoStock 网络响应中断；保留原缓存')
        return value
    bs_socket.send_msg=checked_send
    try:
        login=bs.login()
        if login.error_code!='0': raise RuntimeError(login.error_msg)
        if kind=='bootstrap':
            pipe.send(dict(status='获取交易日历'))
            result=dict(calendar=rows(bs.query_trade_dates(start_date=args['start'],end_date=args['end'])))
            pipe.send(dict(status='获取全市场证券名录'))
            result['securities']=rows(bs.query_stock_basic())
            # The terminated list is slow and changes rarely; omitted on most starts.
            if args.get('terminated',True):
                pipe.send(dict(status='获取退市证券名录'))
                try: result['terminated']=rows(query_terminated_stocks())
                except Exception as ex: result['terminated']=[]; result['terminated_error']=str(ex)
            else: result['terminated']=None
        elif kind=='daily': result=rows(bs.query_daily_history_k_AStock(args['day']))
        elif kind=='history':
            code=args['code']; fields=FIELDS if code!='sh.000300' else 'date,code,open,high,low,close,preclose,volume,amount,pctChg'
            result=dict(bars=rows(bs.query_history_k_data_plus(code,fields,start_date=args['start'],end_date=args['end'],frequency='d',adjustflag='3')),dividends=[])
            if args.get('actions') and code!='sh.000300' and code!='sh.588710':
                for year in range(int(args['start'][:4]),int(args['end'][:4])+1): result['dividends'].extend(rows(bs.query_dividend_data(code,str(year),'operate')))
        else: raise ValueError('未知数据请求')
        pipe.send(dict(ok=True,result=result))
    except Exception as ex: pipe.send(dict(ok=False,error=str(ex)))
    finally:
        pipe.close()
        # The public anonymous login is shared server-side: sending logout in
        # one connection invalidates other anonymous connections. Closing this
        # worker's own socket releases resources without logging out peers.
        try: getattr(bs_context,'default_socket',None).close()
        except Exception: pass

class BaoSource:
    progress=None
    def __init__(self):
        # Public sessions are serialized for reliability; local computations
        # and the independent Sina quote monitor can still run concurrently.
        self._session_lock=threading.Lock()
    def call(self,kind,args,cancel=lambda:False,timeout=150):
        deadline=time.monotonic()+timeout
        while not self._session_lock.acquire(timeout=.1):
            if cancel(): raise InterruptedError('数据任务已暂停，已提交缓存保留')
            if time.monotonic()>=deadline: raise TimeoutError('等待数据源超时；保留缓存，下次重试')
        try:
            return self._call(kind,args,cancel,max(.001,deadline-time.monotonic()))
        finally: self._session_lock.release()
    def _call(self,kind,args,cancel,timeout):
        if cancel(): raise InterruptedError('数据任务已暂停，已提交缓存保留')
        ctx=mp.get_context('spawn'); parent,child=ctx.Pipe(False); process=ctx.Process(target=_bs_worker,args=(child,kind,args),daemon=True)
        process.start(); child.close(); start=time.monotonic()
        try:
            while True:
                if cancel(): raise InterruptedError('数据任务已暂停，已提交缓存保留')
                if time.monotonic()-start>timeout: raise TimeoutError('数据源超时；保留缓存，下次重试')
                while not parent.poll(.15):
                    if cancel(): raise InterruptedError('数据任务已暂停，已提交缓存保留')
                    if time.monotonic()-start>timeout: raise TimeoutError('数据源超时；保留缓存，下次重试')
                    if not process.is_alive(): raise RuntimeError('数据进程提前退出')
                try: value=parent.recv()
                except EOFError as ex: raise RuntimeError('数据进程提前退出；保留缓存') from ex
                if 'status' in value:
                    if self.progress: self.progress(value['status'])
                    continue
                if not value['ok']: raise RuntimeError(value['error'])
                return value['result']
        finally:
            parent.close()
            process.join(.3)
            if process.is_alive(): process.terminate(); process.join(2)

def validate_bars(raw,code=None):
    out=[]; previous=''
    for row in raw:
        if code and row['code']!=code: raise ValueError('证券代码错配')
        day=row['date']; datetime.strptime(day,'%Y-%m-%d')
        if code and day<=previous: raise ValueError('日期重复或乱序')
        previous=day
        b=dict(date=day,code=row['code'],tradestatus=str(row.get('tradestatus','1')),isST=str(row.get('isST','0')))
        for k in ['open','high','low','close','preclose','volume','amount','pctChg']: b[k]=float(row.get(k) or 0)
        if any(not math.isfinite(v) for v in b.values() if isinstance(v,float)): raise ValueError('行情包含非有限数')
        if b['close']<=0 or b['preclose']<=0 or b['volume']<0 or b['amount']<0: raise ValueError('价格或成交量无效')
        if b['tradestatus']=='1' and not 0<b['low']<=min(b['open'],b['close'])<=max(b['open'],b['close'])<=b['high']: raise ValueError('OHLC不一致')
        out.append(b)
    if not out: raise ValueError('数据源返回空数据')
    return out

def parse_quotes(text,fetched=None):
    fetched=fetched or datetime.now(TZ).isoformat(timespec='seconds'); result={}
    for symbol,payload in re.findall(r'var hq_str_(sh\d{6}|sz\d{6})="([^"]*)";',text):
        parts=payload.split(',')
        if len(parts)<32: continue
        try:
            q=dict(code=symbol[:2]+'.'+symbol[2:],name=parts[0],open=float(parts[1]),preclose=float(parts[2]),price=float(parts[3]),high=float(parts[4]),low=float(parts[5]),bid=float(parts[6]),ask=float(parts[7]),volume=float(parts[8]),amount=float(parts[9]),source_time=parts[30]+'T'+parts[31],fetched=fetched,source='新浪公开报价')
            bids=[dict(quantity=float(parts[10+i*2]),price=float(parts[11+i*2])) for i in range(5)]
            asks=[dict(quantity=float(parts[20+i*2]),price=float(parts[21+i*2])) for i in range(5)]
            if any(not math.isfinite(level[k]) or level[k]<0 for level in bids+asks for k in ['quantity','price']): continue
            q.update(bid_volume=bids[0]['quantity'],ask_volume=asks[0]['quantity'],book=dict(bids=bids,asks=asks),raw_fields=parts)
            datetime.fromisoformat(q['source_time'])
            if q['price']<=0 or not all(math.isfinite(v) for v in q.values() if isinstance(v,float)): continue
            result[q['code']]=q
        except (ValueError,IndexError): continue
    return result

def sina_quotes(codes):
    codes=sorted(set(codes))
    if not codes: return {}
    if len(codes)>100: raise ValueError('单次报价限100个候选或持仓')
    symbols=','.join(c.replace('.','') for c in codes)
    request=Request('https://hq.sinajs.cn/list='+symbols,headers={'Referer':'https://finance.sina.com.cn/','User-Agent':'QuantResearch/0.3'})
    # Domestic public data uses a direct connection; stale Windows/environment
    # proxy settings must not make it depend on a VPN. Default TLS verification stays enabled.
    with build_opener(ProxyHandler({})).open(request,timeout=20) as response: text=response.read(250000).decode('gb18030')
    values=parse_quotes(text)
    if not values: raise RuntimeError('公开报价没有有效记录')
    return values
