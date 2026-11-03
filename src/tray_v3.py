"""Windows native notification-area icon; no extra runtime or admin rights."""
import ctypes
import os
import threading
import webbrowser
from ctypes import wintypes as w

class Tray:
    def __init__(self,url,on_start,on_stop,on_exit,on_open=None,icon_path=None):
        self.url=url; self.on_start=on_start; self.on_stop=on_stop; self.on_exit=on_exit; self.hwnd=None; self.error=None; self.last_delivery=None; self.last_plan=None; self.ready=threading.Event()
        self.on_open=on_open; self.icon_path=icon_path
    def start(self):
        if os.name!='nt': self.error='此平台没有Windows托盘'; return
        threading.Thread(target=self.run,daemon=True,name='windows-tray').start(); self.ready.wait(3)
    def run(self):
        try: self._run()
        except Exception as ex: self.error=str(ex); self.ready.set()
    def _run(self):
        user=ctypes.WinDLL('user32',use_last_error=True); shell=ctypes.WinDLL('shell32',use_last_error=True); kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        LRESULT=ctypes.c_ssize_t; WNDPROC=ctypes.WINFUNCTYPE(LRESULT,w.HWND,w.UINT,w.WPARAM,w.LPARAM)
        class WNDCLASS(ctypes.Structure):
            _fields_=[('style',w.UINT),('proc',WNDPROC),('extra',ctypes.c_int),('window_extra',ctypes.c_int),('instance',w.HINSTANCE),('icon',w.HICON),('cursor',w.HANDLE),('background',w.HBRUSH),('menu',w.LPCWSTR),('name',w.LPCWSTR)]
        class GUID(ctypes.Structure): _fields_=[('a',w.DWORD),('b',w.WORD),('c',w.WORD),('d',ctypes.c_byte*8)]
        class DATA(ctypes.Structure):
            _fields_=[('size',w.DWORD),('hwnd',w.HWND),('id',w.UINT),('flags',w.UINT),('callback',w.UINT),('icon',w.HICON),('tip',w.WCHAR*128),('state',w.DWORD),('stateMask',w.DWORD),('info',w.WCHAR*256),('timeout',w.UINT),('title',w.WCHAR*64),('infoFlags',w.DWORD),('guid',GUID),('balloonIcon',w.HICON)]
        user.DefWindowProcW.argtypes=[w.HWND,w.UINT,w.WPARAM,w.LPARAM]; user.DefWindowProcW.restype=LRESULT
        user.CreateWindowExW.argtypes=[w.DWORD,w.LPCWSTR,w.LPCWSTR,w.DWORD,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,w.HWND,w.HMENU,w.HINSTANCE,w.LPVOID]; user.CreateWindowExW.restype=w.HWND
        kernel.GetModuleHandleW.argtypes=[w.LPCWSTR]; kernel.GetModuleHandleW.restype=w.HMODULE
        user.LoadIconW.argtypes=[w.HINSTANCE,w.LPCWSTR]; user.LoadIconW.restype=w.HICON
        user.CreatePopupMenu.restype=w.HMENU; user.AppendMenuW.argtypes=[w.HMENU,w.UINT,ctypes.c_size_t,w.LPCWSTR]
        user.TrackPopupMenu.argtypes=[w.HMENU,w.UINT,ctypes.c_int,ctypes.c_int,ctypes.c_int,w.HWND,ctypes.c_void_p]
        user.SetForegroundWindow.argtypes=[w.HWND]; user.DestroyMenu.argtypes=[w.HMENU]; user.DestroyWindow.argtypes=[w.HWND]
        shell.Shell_NotifyIconW.argtypes=[w.DWORD,ctypes.POINTER(DATA)]; shell.Shell_NotifyIconW.restype=w.BOOL
        def open_plan():
            suffix='/#page='+self.last_plan[5:] if self.last_plan and self.last_plan.startswith('page:') else ('/#plan='+self.last_plan if self.last_plan else '/')
            (self.on_open or webbrowser.open)(self.url+suffix)
        @WNDPROC
        def proc(hwnd,msg,wp,lp):
            if msg==0x801:
                if lp in (0x203,0x405): open_plan()
                elif lp==0x205:
                    menu=user.CreatePopupMenu()
                    for ident,label in [(1,'打开量研'),(2,'开始工作'),(3,'停止工作'),(4,'退出软件')]: user.AppendMenuW(menu,0,ident,label)
                    point=w.POINT(); user.GetCursorPos(ctypes.byref(point)); user.SetForegroundWindow(hwnd)
                    command=user.TrackPopupMenu(menu,0x100|0x2,point.x,point.y,0,hwnd,None); user.DestroyMenu(menu)
                    if command==1: open_plan()
                    elif command==2: self.on_start()
                    elif command==3: self.on_stop()
                    elif command==4: self.on_exit()
                return 0
            if msg==0x10: user.DestroyWindow(hwnd); return 0
            if msg==2: shell.Shell_NotifyIconW(2,ctypes.byref(self.data)); user.PostQuitMessage(0); return 0
            return user.DefWindowProcW(hwnd,msg,wp,lp)
        self.proc=proc; instance=kernel.GetModuleHandleW(None); cls=WNDCLASS(); cls.proc=proc; cls.instance=instance; cls.name='QuantResearchTrayV3'
        if not user.RegisterClassW(ctypes.byref(cls)): raise ctypes.WinError(ctypes.get_last_error())
        self.hwnd=user.CreateWindowExW(0,cls.name,'量研',0,0,0,0,0,None,None,instance,None)
        if not self.hwnd: raise ctypes.WinError(ctypes.get_last_error())
        self.DATA=DATA; self.shell=shell; self.data=DATA(); self.data.size=ctypes.sizeof(DATA); self.data.hwnd=self.hwnd; self.data.id=1; self.data.flags=1|2|4; self.data.callback=0x801; self.data.tip='量研 · 已暂停'
        self.data.icon=user.LoadIconW(None,ctypes.cast(ctypes.c_void_p(32512),w.LPCWSTR))
        if self.icon_path and os.path.isfile(self.icon_path):
            user.LoadImageW.argtypes=[w.HINSTANCE,w.LPCWSTR,w.UINT,ctypes.c_int,ctypes.c_int,w.UINT]; user.LoadImageW.restype=w.HANDLE
            self.data.icon=user.LoadImageW(None,str(self.icon_path),1,32,32,0x10) or self.data.icon
        if not shell.Shell_NotifyIconW(0,ctypes.byref(self.data)): raise ctypes.WinError(ctypes.get_last_error())
        self.ready.set(); message=w.MSG()
        while user.GetMessageW(ctypes.byref(message),None,0,0)>0: user.TranslateMessage(ctypes.byref(message)); user.DispatchMessageW(ctypes.byref(message))
    def status(self,message):
        if not self.hwnd or self.error: return
        data=self.DATA(); data.size=ctypes.sizeof(data); data.hwnd=self.hwnd; data.id=1; data.flags=4; data.tip=('量研 · '+message)[:127]
        self.shell.Shell_NotifyIconW(1,ctypes.byref(data))
    def notify(self,title,message,plan_id=None):
        self.last_plan=plan_id
        if not self.hwnd or self.error: self.last_delivery=False; return False
        data=self.DATA(); data.size=ctypes.sizeof(data); data.hwnd=self.hwnd; data.id=1; data.flags=16; data.info=message[:255]; data.title=title[:63]; data.infoFlags=1; data.timeout=15000
        self.last_delivery=bool(self.shell.Shell_NotifyIconW(1,ctypes.byref(data)))
        if self.last_delivery:
            import winsound
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
        return self.last_delivery
