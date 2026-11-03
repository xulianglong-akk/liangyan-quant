import os
for _name in ['OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS']: os.environ[_name]='2'
import sys
import json
import threading
import sqlite3
import ctypes
import uuid
from datetime import datetime
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse,parse_qs,quote
from contextlib import closing
from store_v3 import Store,dump,now
from runtime_v3 import Runtime,SESSION_NAMES
from tray_v3 import Tray
from rules_v3 import COSTS,PARAMS,TZ
from status_v3 import activity_summary,evolution_progress

ASSETS=Path(getattr(sys,'_MEIPASS',Path(__file__).resolve().parents[1]))
ROOT=Path(sys.executable).resolve().parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parents[1]
PORT=int(os.environ.get('QUANT_PORT','8765'))
DATA_ROOT=Path(os.environ.get('QUANT_DATA_HOME','D:/量化数据'))
STORE=None; RUNTIME=None; TRAY=None; SERVER=None

def memory_mb():
    if os.name!='nt': return None
    from ctypes import wintypes as w
    class MEMORY(ctypes.Structure): _fields_=[('cb',w.DWORD),('PageFaultCount',w.DWORD)]+[(k,ctypes.c_size_t) for k in ['PeakWorkingSetSize','WorkingSetSize','QuotaPeakPagedPoolUsage','QuotaPagedPoolUsage','QuotaPeakNonPagedPoolUsage','QuotaNonPagedPoolUsage','PagefileUsage','PeakPagefileUsage','PrivateUsage']]
    data=MEMORY(); data.cb=ctypes.sizeof(data); kernel=ctypes.WinDLL('kernel32'); kernel.GetCurrentProcess.restype=w.HANDLE
    psapi=ctypes.WinDLL('psapi'); psapi.GetProcessMemoryInfo.argtypes=[w.HANDLE,ctypes.POINTER(MEMORY),w.DWORD]
    return round(data.WorkingSetSize/1048576,2) if psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(),ctypes.byref(data),data.cb) else None

def stop_server():
    RUNTIME.stop(); RUNTIME.shutdown.set()
    threading.Thread(target=SERVER.shutdown,daemon=True).start()

def backup_database(label='v3-backup'):
    folder=DATA_ROOT/'backups'; folder.mkdir(parents=True,exist_ok=True)
    target=folder/(label+'-'+datetime.now(TZ).strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8]+'.sqlite3')
    with STORE.db() as src,closing(sqlite3.connect(target)) as dst: src.backup(dst)
    return str(target)

def invalid_json_number(value): raise ValueError('请求包含无效数值：'+value)

class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup(); self.connection.settimeout(10)
    def log_message(self,*args): pass
    def local(self): return self.headers.get('Host') in [f'127.0.0.1:{PORT}',f'localhost:{PORT}']
    def respond(self,value,status=200):
        raw=dump(value).encode('utf-8'); self.send_response(status); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(raw))); self.end_headers()
        try: self.wfile.write(raw)
        except (BrokenPipeError,ConnectionResetError): pass
    def do_GET(self):
        if not self.local(): return self.respond(dict(error='仅允许本机访问'),403)
        parsed=urlparse(self.path)
        try:
            if parsed.path=='/api/health':
                return self.respond(dict(app='quant-assistant',schema='v3',version='0.4.0',pid=os.getpid(),root=str(ROOT.resolve()),data_root=str(DATA_ROOT.resolve()),db=str(STORE.path.resolve()),active=bool(STORE.get('work')['active'])))
            if parsed.path=='/api/tutorial':
                if parsed.query: return self.respond(dict(error='教程下载不接受文件路径参数'),400)
                document=DATA_ROOT/'outputs'/'量研新手操作教程.docx'
                if not document.is_file(): document=ASSETS/'guide.docx'
                if not document.is_file(): return self.respond(dict(error='教程正在生成，请稍后再试'),404)
                raw=document.read_bytes(); self.send_response(200); self.send_header('Content-Type','application/vnd.openxmlformats-officedocument.wordprocessingml.document'); self.send_header('Content-Disposition',"attachment; filename=Quant-Assistant-Guide.docx; filename*=UTF-8''"+quote(document.name)); self.send_header('Content-Length',str(len(raw))); self.end_headers(); self.wfile.write(raw); return
            if parsed.path=='/api/state':
                value=STORE.summary(); session=RUNTIME.session(); value['session']=SESSION_NAMES[session]; value['activity']=activity_summary(STORE,value['settings']['mode'],session); value['quote_health']=STORE.get('quote_health',{}); value['desktop']=dict(tray=bool(TRAY.hwnd and not TRAY.error),error=TRAY.error,last_delivery=TRAY.last_delivery); return self.respond(value)
            if parsed.path=='/api/page':
                page=parse_qs(parsed.query).get('name',[''])[0]; value=STORE.page(page)
                if page=='paper':
                    for mode in PARAMS:value[mode]['activity']=activity_summary(STORE,mode,RUNTIME.session(),account_id='paper-'+mode)
                if page=='evolution':value['progress']=evolution_progress(STORE)
                if page=='data': value['metrics']['memory_mb']=memory_mb(); value['research_wait']=STORE.get('research_wait','等待历史准备完成'); value['storage']=str(DATA_ROOT)
                return self.respond(value)
            if parsed.path=='/api/plans': return self.respond(STORE.latest_plan(parse_qs(parsed.query).get('account',['paper-steady'])[0]))
            if parsed.path=='/api/quotes': return self.respond(STORE.get_quotes())
            if parsed.path=='/api/plan':
                ident=parse_qs(parsed.query).get('id',[''])[0].split(':')[0]
                with STORE.db() as db: row=db.execute('SELECT payload FROM plans WHERE id=?',(ident,)).fetchone()
                if not row: raise ValueError('计划不存在')
                return self.respond(json.loads(row[0]))
            path=(ASSETS/'web'/('index.html' if parsed.path=='/' else parsed.path.lstrip('/'))).resolve()
            if not path.is_relative_to(ASSETS/'web') or not path.is_file(): return self.respond(dict(error='文件不存在'),404)
            raw=path.read_bytes(); self.send_response(200); self.send_header('Content-Type',{'.html':'text/html','.js':'text/javascript','.css':'text/css','.svg':'image/svg+xml'}.get(path.suffix,'application/octet-stream')+'; charset=utf-8'); self.send_header('Content-Length',str(len(raw))); self.send_header('Cache-Control','no-cache'); self.end_headers(); self.wfile.write(raw)
        except Exception as ex: self.respond(dict(error=str(ex)),400)
    def do_POST(self):
        def rejected(message):
            # Drain a bounded body before closing, so Windows does not reset the
            # connection and hide the useful 403 response. Never execute it.
            try:
                size=int(self.headers.get('Content-Length',0))
                if 0<size<=100000: self.rfile.read(size)
            except (ValueError,OSError): pass
            return self.respond(dict(error=message),403)
        if not self.local() or self.headers.get('X-Quant-Local')!='1': return rejected('拒绝非本机写入')
        origin=self.headers.get('Origin')
        if origin and origin not in [f'http://127.0.0.1:{PORT}',f'http://localhost:{PORT}']: return rejected('拒绝跨站写入')
        try:
            length=int(self.headers.get('Content-Length',0))
            if not 0<=length<=100000: raise ValueError('请求大小无效')
            body=json.loads(self.rfile.read(length) or b'{}',parse_constant=invalid_json_number)
            if not isinstance(body,dict): raise ValueError('请求须为对象格式')
            path=urlparse(self.path).path
            if path=='/api/work/start': result=RUNTIME.start()
            elif path=='/api/work/stop': result=RUNTIME.stop()
            elif path=='/api/data/retry': result=RUNTIME.request_data_retry()
            elif path=='/api/research/run':
                force=body.get('force',False)
                if not isinstance(force,bool): raise ValueError('force 须为布尔值')
                result=RUNTIME.request_research(force)
            elif path=='/api/settings':
                with STORE.lock:
                    settings=STORE.get('settings'); new=settings.copy()
                    for k in body:
                        if k not in ['mode','notifications','quote_interval','costs']: raise ValueError('不支持的设置')
                    new.update(body)
                    if new['mode'] not in PARAMS or not isinstance(new['notifications'],bool): raise ValueError('模式或通知设置无效')
                    if not isinstance(new['quote_interval'],int) or not 60<=new['quote_interval']<=300: raise ValueError('刷新间隔须为60至300秒')
                    if set(new['costs'])!=set(COSTS): raise ValueError('费用字段不完整')
                    for k,v in new['costs'].items():
                        import math
                        if isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or v<0 or v>(100 if k=='minimum' else .05): raise ValueError('费用参数无效')
                    if new['costs']['participation']<=0: raise ValueError('流动性参数须大于0')
                    if new['costs']!=settings['costs'] and STORE.get('work')['active']: raise ValueError('修改费用前请先停止工作')
                    if new['costs']!=settings['costs']:
                        with STORE.db() as db:
                            for row in db.execute("SELECT id,payload FROM strategies WHERE status='current'").fetchall():
                                payload=json.loads(row['payload']); payload['evidence']=None; payload['forward_evidence']=None
                                db.execute('UPDATE strategies SET payload=? WHERE id=?',(dump(payload),row['id']))
                            db.execute("UPDATE strategies SET status='failed' WHERE status='challenger'")
                        STORE.audit('费用模型变更，需重新验证',new['costs'])
                    STORE.set('settings',new); STORE.audit('修改设置',body); result=new
            elif path=='/api/real/trades': result=STORE.real_trade(body)
            elif path=='/api/real/cash-adjust': result=STORE.real_cash_adjust(body,before_change=lambda:backup_database('before-cash-check'))
            elif path=='/api/real/undo-last': result=STORE.undo_last_real_trade(body,before_change=lambda:backup_database('before-record-correction'))
            elif path=='/api/real/enable':
                if not STORE.readiness()['ready']: raise ValueError('尚未通过历史及后续模拟观察门槛')
                settings=STORE.get('settings')
                if not settings['source_verified']: raise ValueError('行情用途与盘中稳定性尚未核实，请先在数据设置登记核实依据')
                settings['stage']='real'; STORE.set('settings',settings); STORE.audit('用户启用实盘参考',now()); result=dict(ok=True)
            elif path=='/api/source/verify':
                evidence=body.get('evidence','').strip()
                if len(evidence)<20: raise ValueError('请记录来源用途授权及盘中稳定性核实依据，不能仅填“确认”')
                STORE.set('source_evidence',dict(time=now(),evidence=evidence)); settings=STORE.get('settings'); settings['source_verified']=True; STORE.set('settings',settings); STORE.audit('用户登记数据源核实依据',evidence); result=dict(ok=True)
            elif path=='/api/rollback': RUNTIME.rollback(body['mode'],body['id']); result=dict(ok=True)
            elif path=='/api/notifications/test': result=dict(submitted=TRAY.notify('量研提醒测试','收到此提醒后，点击可打开软件。'))
            elif path=='/api/backup':
                result=dict(file=backup_database())
            elif path=='/api/shutdown': stop_server(); result=dict(ok=True)
            else: return self.respond(dict(error='接口不存在'),404)
            TRAY.status('工作中' if STORE.get('work')['active'] else '已暂停'); return self.respond(result)
        except Exception as ex: self.respond(dict(error=str(ex)),400)

def main(on_ready=None,on_open=None,on_exit=None):
    global STORE,RUNTIME,TRAY,SERVER
    for name in ['work','outputs','data','backups']: (DATA_ROOT/name).mkdir(parents=True,exist_ok=True)
    STORE=Store(os.environ.get('QUANT_DB',str(DATA_ROOT/'data'/'assistant-v3.sqlite3')),DATA_ROOT/'data'/'research.sqlite3')
    if PARAMS['steady'].get('quality'): STORE.install_requested_seed('v3.1-seed',PARAMS)
    RUNTIME=Runtime(STORE,DATA_ROOT); SERVER=ThreadingHTTPServer(('127.0.0.1',PORT),Handler)
    TRAY=Tray(f'http://127.0.0.1:{PORT}',RUNTIME.start,RUNTIME.stop,on_exit or stop_server,on_open=on_open,icon_path=ASSETS/'app.ico'); RUNTIME.notifier=TRAY
    if os.environ.get('QUANT_NO_TRAY')!='1': TRAY.start()
    if on_ready: on_ready()
    print(f'量研 v0.4 http://127.0.0.1:{PORT} · 等待开始工作',flush=True)
    try: SERVER.serve_forever()
    except KeyboardInterrupt: RUNTIME.stop()
    finally:
        RUNTIME.shutdown.set(); RUNTIME.cancel.set(); SERVER.server_close()

if __name__=='__main__':
    import multiprocessing
    multiprocessing.freeze_support(); main()
