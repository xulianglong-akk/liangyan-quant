"""Windows desktop entry. Frozen multiprocessing is diverted before GUI imports."""
import multiprocessing

if __name__=='__main__':
    multiprocessing.freeze_support()

import argparse
import ctypes
import hashlib
import json
import logging
import os
from pathlib import Path
import socket
import sys
import threading
import time
import traceback
import urllib.request

TITLE='量研 · 量化交易助手'
VERSION='0.4.0'
OPENER=urllib.request.build_opener(urllib.request.ProxyHandler({}))

def assets():
    return Path(getattr(sys,'_MEIPASS',Path(__file__).resolve().parents[1]))

def profile_dir():
    return Path(os.environ.get('QUANT_CONFIG_HOME',str(Path(os.environ.get('LOCALAPPDATA',Path.home()))/'LiangYan')))

def data_directory(explicit=None):
    if explicit: return Path(explicit).expanduser().resolve()
    if os.environ.get('QUANT_DATA_HOME'): return Path(os.environ['QUANT_DATA_HOME']).resolve()
    if os.name=='nt':
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,'Software\\LiangYan\\LiangYan') as key:
                return Path(winreg.QueryValueEx(key,'DataDirectory')[0]).resolve()
        except FileNotFoundError: pass
    config=profile_dir()/'desktop.json'
    if config.is_file():
        value=json.loads(config.read_text(encoding='utf-8-sig'))
        if value.get('data_root'): return Path(value['data_root']).expanduser().resolve()
    return Path('D:/量化数据') if Path('D:/').is_dir() else Path.home()/'Documents'/'量研数据'

def request(port,path='health',body=None):
    if path=='health':
        # Windows may take a second to reject each closed TCP port. Avoid a
        # long scan while retaining the full HTTP timeout for live services.
        with socket.create_connection(('127.0.0.1',port),timeout=.08):pass
    req=urllib.request.Request(f'http://127.0.0.1:{port}/api/'+path)
    if body is not None:
        req.data=json.dumps(body).encode(); req.add_header('X-Quant-Local','1'); req.add_header('Content-Type','application/json')
    with OPENER.open(req,timeout=3) as response: return json.load(response)

def matching_health(port,data):
    try:
        value=request(port)
        if isinstance(value,dict) and isinstance(value.get('data_root'),str) and value.get('app')=='quant-assistant' and value.get('schema')=='v3' and Path(value['data_root']).resolve()==data.resolve(): return value
    except (OSError,ValueError,KeyError,TypeError): pass
    return None

def choose_service(data,preferred=None):
    ports=[]
    marker=data/'work'/'desktop-service.json'
    if marker.is_file():
        try:
            saved=json.loads(marker.read_text(encoding='utf-8'))
            port=int(saved['port'])
            if 1<=port<=65535: ports.append(port)
        except (ValueError,KeyError,TypeError): pass
    if preferred: ports.append(preferred)
    ports.extend(range(8765,8796))
    for port in dict.fromkeys(ports):
        health=matching_health(port,data)
        if health: return port,health
    candidates=[preferred] if preferred else range(8765,8796)
    for port in candidates:
        with socket.socket() as check:
            try: check.bind(('127.0.0.1',port))
            except OSError: continue
            return port,None
    raise RuntimeError('本机服务端口被占用，请关闭旧版量研后重新打开。')

class Instance:
    def __init__(self,data):
        from ctypes import wintypes as w
        self.kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        self.kernel.CreateMutexW.argtypes=[ctypes.c_void_p,w.BOOL,w.LPCWSTR]; self.kernel.CreateMutexW.restype=w.HANDLE
        self.kernel.CloseHandle.argtypes=[w.HANDLE]
        digest=hashlib.sha256(str(data).casefold().encode()).hexdigest()[:24]
        self.handle=self.kernel.CreateMutexW(None,False,'Local\\LiangYanDesktop-'+digest)
        if not self.handle: raise ctypes.WinError(ctypes.get_last_error())
        self.duplicate=ctypes.get_last_error()==183
    def close(self):
        if self.handle: self.kernel.CloseHandle(self.handle); self.handle=None

def open_request(data,url=None):
    work=data/'work'; work.mkdir(parents=True,exist_ok=True)
    temporary=work/('open-request-'+str(os.getpid())+'.tmp')
    temporary.write_text(json.dumps(dict(url=url,time=time.time())),encoding='utf-8')
    temporary.replace(work/'open-request.json')

def setup_logging(data):
    from logging.handlers import RotatingFileHandler
    folder=data/'work'; folder.mkdir(parents=True,exist_ok=True)
    handler=RotatingFileHandler(folder/'desktop.log',maxBytes=2*1024*1024,backupCount=2,encoding='utf-8')
    logging.basicConfig(level=logging.INFO,handlers=[handler],force=True)
    for name in ('stdout','stderr'):
        if getattr(sys,name) is None: setattr(sys,name,(folder/('desktop-'+name+'.log')).open('a',encoding='utf-8',buffering=1))

def show_error(message):
    ctypes.windll.user32.MessageBoxW(None,str(message),'量研',0x10)

def run_desktop(data,preferred=None,open_url=None,smoke_seconds=None):
    for name in ('data','work','outputs','backups'): (data/name).mkdir(parents=True,exist_ok=True)
    setup_logging(data); instance=Instance(data)
    if instance.duplicate:
        open_request(data,open_url); instance.close(); return
    quitting=threading.Event(); ready=threading.Event(); errors=[]; window=None; service=None; server_thread=None
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('LiangYan.Quant.Desktop')
        port,existing=choose_service(data,preferred)
        resume=bool(existing and existing.get('active'))
        if existing:
            # Hand over only a verified instance using this exact data directory.
            request(port,'shutdown',{})
            deadline=time.monotonic()+15
            while matching_health(port,data) and time.monotonic()<deadline: time.sleep(.15)
            if matching_health(port,data): raise RuntimeError('旧后台尚未退出，请在旧版托盘退出软件后重新打开。')
            existing=None
        base=f'http://127.0.0.1:{port}'
        def reveal(url=None):
            if window is not None:
                if url and url.startswith(base+'/#'): window.load_url(url)
                window.show(); window.restore()
        def exit_app():
            if quitting.is_set(): return
            quitting.set()
            try:
                if service is not None: service.stop_server()
                elif existing: request(port,'shutdown',{})
            except OSError: logging.exception('停止后台时的错误')
            if window is not None: window.destroy()
        if not existing:
            os.environ['QUANT_DATA_HOME']=str(data); os.environ['QUANT_PORT']=str(port)
            os.environ['QUANT_EMPTY_REAL']='1'; os.environ.pop('QUANT_DB',None)
            for name in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS'): os.environ[name]='2'
            import main as service
            def serve():
                try: service.main(on_ready=ready.set,on_open=reveal,on_exit=exit_app)
                except Exception:
                    errors.append(traceback.format_exc()); logging.exception('启动后台失败'); ready.set()
            server_thread=threading.Thread(target=serve,daemon=True,name='desktop-service'); server_thread.start()
            if not ready.wait(20): raise RuntimeError('后台启动超时。请查看数据目录work里的desktop.log。')
            if errors: raise RuntimeError(errors[0])
            if resume: service.RUNTIME.start()
        import webview
        webview.settings['ALLOW_DOWNLOADS']=True
        # A WebView2 window retains the existing simple UI without browser chrome.
        initial=open_url if open_url and open_url.startswith(base+'/#') else base+'/'
        window=webview.create_window(TITLE,initial,width=1180,height=820,min_size=(900,620),background_color='#F5F7FA',text_select=True)
        if smoke_seconds:
            def loaded_check():
                try:
                    value=window.evaluate_js("JSON.stringify({title:document.title,button:document.getElementById('work-button').textContent,text:document.body.innerText})")
                    dom=json.loads(value)
                    assert dom['button'] in ('开始工作','停止工作') and '虚拟资金模拟' in dom['text']
                    import clr
                    from System import Action
                    from System.IO import FileStream,FileMode
                    from Microsoft.Web.WebView2.Core import CoreWebView2CapturePreviewImageFormat
                    capture_done=threading.Event(); tasks=[]
                    def capture():
                        stream=FileStream(str(data/'work'/'desktop-window.png'),FileMode.Create)
                        task=window.native.webview.CoreWebView2.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png,stream)
                        tasks.append((task,stream));capture_done.set()
                    window.native.Invoke(Action(capture))
                    assert capture_done.wait(5)
                    task,stream=tasks[0]
                    try:assert task.Wait(8000)
                    finally:stream.Dispose()
                    (data/'work'/'desktop-dom.json').write_text(json.dumps(dict(passed=True,title=dom['title'],button=dom['button']),ensure_ascii=False),encoding='utf-8')
                except Exception:(data/'work'/'desktop-dom.json').write_text(json.dumps(dict(passed=False,error=traceback.format_exc())),encoding='utf-8')
            window.events.loaded+=loaded_check
        def closing():
            if quitting.is_set(): return True
            tray=service.TRAY if service is not None else None
            if tray is None or tray.error or not tray.hwnd:
                exit_app(); return True
            window.hide(); return False
        window.events.closing+=closing
        (data/'work'/'desktop-service.json').write_text(json.dumps(dict(port=port,pid=os.getpid(),version=VERSION,data_root=str(data))),encoding='utf-8')
        started=time.monotonic()
        def watch():
            marker=data/'work'/'open-request.json'
            while not quitting.wait(.4):
                if marker.is_file():
                    try: value=json.loads(marker.read_text(encoding='utf-8')); marker.unlink(); reveal(value.get('url'))
                    except (OSError,ValueError): logging.exception('打开窗口请求失败')
                if server_thread and not server_thread.is_alive(): exit_app(); return
                if smoke_seconds and time.monotonic()-started>=smoke_seconds:
                    (data/'work'/'desktop-gui-test.json').write_text(json.dumps(dict(window_created=True,version=VERSION,health=matching_health(port,data))),encoding='utf-8')
                    exit_app(); return
        webview.start(watch,gui='edgechromium',debug=False,private_mode=False,storage_path=str(data/'work'/'webview'),icon=str(assets()/'app.ico'))
    finally:
        quitting.set()
        if service and service.RUNTIME:
            service.RUNTIME.stop(); service.RUNTIME.shutdown.set()
            if service.SERVER: service.SERVER.shutdown()
        if server_thread: server_thread.join(5)
        instance.close()

def main_entry():
    parser=argparse.ArgumentParser()
    parser.add_argument('--data-dir'); parser.add_argument('--port',type=int); parser.add_argument('--open')
    parser.add_argument('--shutdown',action='store_true'); parser.add_argument('--self-test'); parser.add_argument('--smoke-seconds',type=int)
    args=parser.parse_args()
    if args.self_test:
        from desktop_selftest import run
        return run(Path(args.self_test))
    data=data_directory(args.data_dir)
    if args.shutdown:
        port,health=choose_service(data,args.port)
        if health: request(port,'shutdown',{})
        return
    try: run_desktop(data,args.port,args.open,args.smoke_seconds)
    except Exception:
        detail=traceback.format_exc(); logging.exception('量研未能打开')
        (data/'work').mkdir(parents=True,exist_ok=True)
        (data/'work'/'desktop-start-error.txt').write_text(detail,encoding='utf-8')
        show_error('未能打开量研。\n请查看 '+str(data/'work'/'desktop-start-error.txt')+'\n\n'+str(sys.exc_info()[1]))
        return 1
    return 0

if __name__=='__main__':
    sys.exit(main_entry())
