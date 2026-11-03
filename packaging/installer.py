"""Per-user Windows setup with native WinForms wizard and manifest-safe uninstall."""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
import winreg
import zipfile

VERSION='0.4.0'
PRODUCT='Software\\LiangYan\\LiangYan'
UNINSTALL='Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\LiangYan.Quant'
ASSETS=Path(getattr(sys,'_MEIPASS',Path(__file__).resolve().parent))
MANIFEST='liangyan-install.json'

def registered(key,name,default=''):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,key) as h:return winreg.QueryValueEx(h,name)[0]
    except FileNotFoundError:return default

def initial_paths():
    program=registered(PRODUCT,'InstallDirectory') or ('E:\\量研' if Path('E:/').is_dir() else str(Path(os.environ['LOCALAPPDATA'])/'Programs'/'量研'))
    data=registered(PRODUCT,'DataDirectory') or ('D:\\量化数据' if Path('D:/').is_dir() else str(Path.home()/'Documents'/'量研数据'))
    return program,data

def validate_paths(program,data):
    if not Path(program).is_absolute() or not Path(data).is_absolute():raise ValueError('请输入包含盘符的完整文件夹路径。')
    program=Path(program).expanduser().resolve();data=Path(data).expanduser().resolve()
    if program==Path(program.anchor) or data==Path(data.anchor):raise ValueError('请选具体文件夹，不要只选择盘符。')
    if program==data or data.is_relative_to(program) or program.is_relative_to(data):raise ValueError('程序与数据请选择两个独立的文件夹。')
    manifest=program/MANIFEST
    if program.exists() and any(program.iterdir()) and not manifest.is_file():raise ValueError('这个程序文件夹已有其他内容。请选择一个空文件夹，或原量研安装文件夹。')
    if manifest.is_file():
        value=json.loads(manifest.read_text(encoding='utf-8'))
        if value.get('format')!='liangyan-install-v1' or Path(value['root']).resolve()!=program:raise ValueError('原安装标记不匹配，已保留现有文件。')
    return program,data

def safe_member(root,name):
    path=(root/name).resolve()
    if not path.is_relative_to(root) or name.startswith(('/', '\\')) or ':' in name:raise ValueError('安装包包含无效路径')
    return path

def webview_available():
    key='Software\\Microsoft\\EdgeUpdate\\Clients\\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
    for hive,view in [(winreg.HKEY_CURRENT_USER,0),(winreg.HKEY_LOCAL_MACHINE,winreg.KEY_WOW64_32KEY),(winreg.HKEY_LOCAL_MACHINE,winreg.KEY_WOW64_64KEY)]:
        try:
            with winreg.OpenKey(hive,key,0,winreg.KEY_READ|view) as h:
                if winreg.QueryValueEx(h,'pv')[0] not in ('','0.0.0.0'):return True
        except FileNotFoundError:pass
    return False

def known_folder(which):
    b=ctypes.create_unicode_buffer(32768)
    if ctypes.windll.shell32.SHGetFolderPathW(None,which,None,0,b):raise OSError('无法读取Windows文件夹位置')
    return Path(b.value)

def shortcut(path,target,args=''):
    def literal(s):return "'"+str(s).replace("'","''")+"'"
    script="\n".join(["$s=New-Object -ComObject WScript.Shell",'$l=$s.CreateShortcut('+literal(path)+')', '$l.TargetPath='+literal(target),'$l.Arguments='+literal(args),'$l.WorkingDirectory='+literal(Path(target).parent),'$l.IconLocation='+literal(str(target)+',0'),'$l.Save()'])
    subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',script],creationflags=subprocess.CREATE_NO_WINDOW,check=True,capture_output=True)

def shortcut_owned(path,root):
    if not path.is_file():return False
    literal="'"+str(path).replace("'","''")+"'"
    result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',"[Console]::OutputEncoding=[Text.Encoding]::UTF8;$s=New-Object -ComObject WScript.Shell;$s.CreateShortcut("+literal+").TargetPath"],creationflags=subprocess.CREATE_NO_WINDOW,check=True,capture_output=True)
    target=result.stdout.decode('utf-8-sig').strip()
    return bool(target) and Path(target).resolve().is_relative_to(root)

def shutdown_previous(program,data):
    executable=program/'量研.exe'
    if executable.is_file():
        try:
            with executable.open('r+b'):pass
            return
        except PermissionError:pass
        subprocess.run([str(executable),'--shutdown','--data-dir',str(data)],creationflags=subprocess.CREATE_NO_WINDOW,timeout=25,check=True)
        # The bootloader parent can hold the executable briefly after the UI exits.
        deadline=time.monotonic()+25
        while True:
            try:
                with executable.open('r+b'):pass
                return
            except PermissionError:
                if time.monotonic()>=deadline:raise RuntimeError('原量研尚未完全退出。请从托盘退出后重新安装。')
                time.sleep(.2)

def write_manifest(path,value):
    temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
        temporary.replace(path)
    finally:temporary.unlink(missing_ok=True)

def install_identity(test_profile=None):
    if test_profile:
        identity='Tests\\'+hashlib.sha256(str(test_profile).encode()).hexdigest()[:16]
        return PRODUCT+'\\'+identity,UNINSTALL+'.'+identity.replace('\\','.'),Path(test_profile)/'桌面',Path(test_profile)/'开始菜单'
    return PRODUCT,UNINSTALL,known_folder(0),known_folder(2)/'量研'

def install(program,data,progress=lambda value,text:None,test_profile=None):
    if sys.maxsize<2**32 or sys.getwindowsversion().major<10:raise ValueError('此安装包支持Windows 10/11的64位系统。')
    program,data=validate_paths(program,data)
    previous=program/MANIFEST
    old=json.loads(previous.read_text(encoding='utf-8')) if previous.is_file() else {}
    recorded=old.get('files',[])
    product,uninstall_key,desktop,menu=install_identity(test_profile)
    archive=ASSETS/'application.zip';runtime=ASSETS/'WebView2-Standalone-X64.exe'
    expected=json.loads((ASSETS/'payload-checksums.json').read_text(encoding='utf-8'))
    for f in (archive,runtime):
        if hashlib.sha256(f.read_bytes()).hexdigest()!=expected[f.name]:raise ValueError('安装包内容校验失败，请重新复制完整的安装程序。')
    with zipfile.ZipFile(archive) as z:
        members=[m for m in z.infolist() if not m.is_dir()]
        owned={str(safe_member(program,name)).casefold() for name in recorded}
        for m in members:
            target=safe_member(program,m.filename)
            if target.exists() and str(target).casefold() not in owned:raise ValueError('安装位置有同名的其他文件，已保留：'+m.filename)
        if z.testzip():raise ValueError('安装包文件损坏。')
    shutdown_previous(program,Path(old.get('data',str(data))).resolve())
    data.mkdir(parents=True,exist_ok=True);program.mkdir(parents=True,exist_ok=True)
    progress(4,'检查Windows图形运行组件')
    if not webview_available():
        code=subprocess.run([str(runtime),'/silent','/install'],creationflags=subprocess.CREATE_NO_WINDOW,timeout=300).returncode
        if code not in (0,3010) or not webview_available():raise RuntimeError('Windows图形运行组件安装未完成。请重启电脑后重试；仍失败时按安装说明从微软官网下载WebView2。')
    files=[]
    provisional=dict(format='liangyan-install-v1',version=VERSION,root=str(program),data=str(data),files=recorded,shortcuts=old.get('shortcuts',[]),product_key=product,uninstall_key=uninstall_key)
    write_manifest(previous,provisional)
    with zipfile.ZipFile(archive) as z:
        for i,m in enumerate(members):
            target=safe_member(program,m.filename);target.parent.mkdir(parents=True,exist_ok=True)
            files.append(m.filename)
            provisional['files']=list(dict.fromkeys(recorded+files));write_manifest(previous,provisional)
            with z.open(m) as src,target.open('wb') as dst:shutil.copyfileobj(src,dst)
            if i%10==0:
                progress(5+int(85*(i+1)/len(members)),'安装量研程序文件')
    desktop.mkdir(parents=True,exist_ok=True);menu.mkdir(parents=True,exist_ok=True)
    executable=program/'量研.exe';uninstaller=program/'卸载量研.exe'
    shortcut(desktop/'量研.lnk',executable)
    shortcut(menu/'量研.lnk',executable)
    shortcut(menu/'卸载量研.lnk',uninstaller,'--uninstall')
    manifest=dict(format='liangyan-install-v1',version=VERSION,root=str(program),data=str(data),files=list(dict.fromkeys(recorded+files)),shortcuts=list(dict.fromkeys(old.get('shortcuts',[])+[str(desktop/'量研.lnk'),str(menu/'量研.lnk'),str(menu/'卸载量研.lnk')])),product_key=product,uninstall_key=uninstall_key)
    write_manifest(program/MANIFEST,manifest)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER,product) as h:
        for name,value in [('InstallDirectory',str(program)),('DataDirectory',str(data)),('Version',VERSION)]:winreg.SetValueEx(h,name,0,winreg.REG_SZ,value)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER,uninstall_key) as h:
        for name,value in [('DisplayName','量研'),('DisplayVersion',VERSION),('Publisher','量研项目'),('InstallLocation',str(program)),('DisplayIcon',str(executable)+',0'),('UninstallString','"'+str(uninstaller)+'" --uninstall'),('QuietUninstallString','"'+str(uninstaller)+'" --uninstall --silent')]:winreg.SetValueEx(h,name,0,winreg.REG_SZ,value)
        winreg.SetValueEx(h,'NoModify',0,winreg.REG_DWORD,1);winreg.SetValueEx(h,'NoRepair',0,winreg.REG_DWORD,1)
    progress(100,'安装完成')
    return manifest

def uninstall(program=None,progress=lambda value,text:None):
    root=Path(program or Path(sys.executable).parent).resolve();file=root/MANIFEST
    if not file.is_file():raise ValueError('没有找到量研安装记录，未删除任何文件。')
    value=json.loads(file.read_text(encoding='utf-8'))
    if value.get('format')!='liangyan-install-v1' or Path(value['root']).resolve()!=root or root==Path(root.anchor):raise ValueError('安装位置不匹配，未删除文件。')
    data=Path(value['data']).resolve()
    if data==root or data.is_relative_to(root):raise ValueError('数据位置需要先迁移，已保留所有文件。')
    shutdown_previous(root,data)
    own=Path(sys.executable).resolve();directories=set();deferred=None
    for i,name in enumerate(value['files']):
        target=safe_member(root,name)
        if target==own:deferred=target;continue
        if target.is_file():target.unlink()
        parent=target.parent
        while parent!=root and parent.is_relative_to(root):directories.add(parent);parent=parent.parent
        if i%10==0:progress(int(95*(i+1)/len(value['files'])),'卸载程序文件，保留行情和账户数据')
    for path in sorted(directories,key=lambda p:len(p.parts),reverse=True):
        try:path.rmdir()
        except OSError:pass
    for name in value['shortcuts']:
        path=Path(name)
        if path.suffix.lower()=='.lnk' and path.name in ('量研.lnk','卸载量研.lnk') and shortcut_owned(path,root):path.unlink(missing_ok=True)
    if registered(value['uninstall_key'],'InstallLocation')==str(root):
        try:winreg.DeleteKey(winreg.HKEY_CURRENT_USER,value['uninstall_key'])
        except FileNotFoundError:pass
    # DataDirectory is retained so reinstall can reuse the saved database.
    if registered(value['product_key'],'InstallDirectory')==str(root):
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER,value['product_key']) as h:
            for name in ('InstallDirectory','Version'):
                try:winreg.DeleteValue(h,name)
                except FileNotFoundError:pass
    if deferred:
        cleanup=data/'work'/('uninstall-cleanup-'+uuid.uuid4().hex+'.ps1');cleanup.parent.mkdir(parents=True,exist_ok=True)
        def literal(p):return "'"+str(p).replace("'","''")+"'"
        cleanup.write_text('\n'.join([f'Wait-Process -Id {os.getpid()} -ErrorAction SilentlyContinue',f'for ($quantCleanupAttempt=0; $quantCleanupAttempt -lt 300; $quantCleanupAttempt++) {{',f'  if (-not (Test-Path -LiteralPath {literal(deferred)})) {{ break }}',f'  try {{ Remove-Item -LiteralPath {literal(deferred)} -ErrorAction Stop }} catch {{ Start-Sleep -Milliseconds 200 }}','}',f'if (-not (Test-Path -LiteralPath {literal(deferred)})) {{',f'  Remove-Item -LiteralPath {literal(file)} -ErrorAction SilentlyContinue',f'  if ((Get-ChildItem -LiteralPath {literal(root)} -Force | Measure-Object).Count -eq 0) {{ Remove-Item -LiteralPath {literal(root)} }}','}']),encoding='utf-8-sig')
        subprocess.Popen(['powershell.exe','-NoProfile','-NonInteractive','-Command',cleanup.read_text(encoding='utf-8-sig')],creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        file.unlink()
        try:root.rmdir()
        except OSError:pass
    progress(100,'卸载完成，数据已保留')
    return dict(removed=True,data_preserved=data.is_dir(),data=str(data))

def wizard_window(uninstall_mode=False,smoke_root=None):
    import clr
    clr.AddReference('System.Windows.Forms');clr.AddReference('System.Drawing')
    from System import Action
    from System.Drawing import Size,Point,Font,Icon
    from System.Windows.Forms import Application,Form,Label,TextBox,Button,FolderBrowserDialog,ProgressBar,MessageBox,DialogResult,FormBorderStyle,FormStartPosition
    Application.EnableVisualStyles()
    form=Form();form.Text='量研卸载程序' if uninstall_mode else '量研安装程序';form.Size=Size(610,445);form.StartPosition=FormStartPosition.CenterScreen;form.FormBorderStyle=FormBorderStyle.FixedDialog;form.MaximizeBox=False
    form.Font=Font('Microsoft YaHei',9)
    icon=ASSETS/'app.ico'
    if icon.is_file():form.Icon=Icon(str(icon))
    def label(text,x,y,width=535,height=30):
        obj=Label();obj.Text=text;obj.Location=Point(x,y);obj.Size=Size(width,height);form.Controls.Add(obj);return obj
    label('卸载量研' if uninstall_mode else '安装量研 0.4.0',25,20,height=40).Font=Font('Microsoft YaHei',16)
    label('只卸载程序，行情、模拟和真实账户数据会保留。' if uninstall_mode else '选择程序和数据的位置。安装完成后，双击桌面图标即可使用。',25,70,height=42)
    program,data=initial_paths();inputs=[];browse_buttons=[]
    if smoke_root:program=str(Path(smoke_root)/'App');data=str(Path(smoke_root)/'Data')
    if not uninstall_mode:
        for title,default,y in [('程序安装到',program,130),('行情和账户数据保存在',data,212)]:
            label(title,25,y);box=TextBox();box.Text=default;box.Location=Point(25,y+27);box.Size=Size(455,27);form.Controls.Add(box);inputs.append(box)
            browse=Button();browse.Text='浏览';browse.Location=Point(490,y+25);browse.Size=Size(70,30);form.Controls.Add(browse)
            browse_buttons.append(browse)
            def choose(sender,event,box=box):
                dialog=FolderBrowserDialog();dialog.Description='选择文件夹';dialog.SelectedPath=box.Text
                if dialog.ShowDialog()==DialogResult.OK:box.Text=dialog.SelectedPath
            browse.Click+=choose
    bar=ProgressBar();bar.Location=Point(25,300);bar.Size=Size(535,18);form.Controls.Add(bar)
    status=label('支持Windows 10/11的64位电脑。无需另外安装Python。' if not uninstall_mode else '',25,323,height=28)
    go=Button();go.Text='卸载' if uninstall_mode else '安装';go.Location=Point(383,359);go.Size=Size(85,32);form.Controls.Add(go)
    cancel=Button();cancel.Text='取消';cancel.Location=Point(478,359);cancel.Size=Size(85,32);form.Controls.Add(cancel);cancel.Click+=lambda sender,event:form.Close()
    done=False
    def trace(text):
        if smoke_root:
            with (Path(smoke_root)/'gui-trace.log').open('a',encoding='utf-8') as f:f.write(text+'\n')
    def guard_close(sender,event):
        if not go.Enabled and not done:event.Cancel=True
    form.FormClosing+=guard_close
    def update(value,text):form.BeginInvoke(Action(lambda:(setattr(bar,'Value',value),setattr(status,'Text',text))))
    def click(sender,event):
        nonlocal done
        trace('click')
        if done:
            if not uninstall_mode:subprocess.Popen([str(Path(inputs[0].Text)/'量研.exe')],creationflags=subprocess.CREATE_NO_WINDOW)
            form.Close();return
        go.Enabled=False;cancel.Enabled=False
        for box in inputs+browse_buttons:box.Enabled=False
        def task():
            nonlocal done
            trace('install-thread')
            try:
                if uninstall_mode:uninstall(progress=update)
                else:install(inputs[0].Text,inputs[1].Text,progress=update,test_profile=smoke_root)
                done=True
                def finish():
                    bar.Value=100
                    go.Text='完成' if uninstall_mode else '打开量研';go.Enabled=True;cancel.Text='关闭';cancel.Enabled=True
                    if smoke_root:
                        from System.Drawing import Bitmap,Graphics
                        from System.Drawing.Imaging import ImageFormat
                        from System.Drawing import Rectangle
                        image=Bitmap(form.Width,form.Height);form.DrawToBitmap(image,Rectangle(0,0,form.Width,form.Height));image.Save(str(Path(smoke_root)/'installer-window.png'),ImageFormat.Png);image.Dispose()
                        (Path(smoke_root)/'gui-install.json').write_text(json.dumps(dict(passed=True,version=VERSION,program=inputs[0].Text,data=inputs[1].Text)),encoding='utf-8')
                        form.Close()
                form.BeginInvoke(Action(finish))
            except Exception as ex:
                message=str(ex)
                if smoke_root:(Path(smoke_root)/'gui-failure.json').write_text(json.dumps(dict(passed=False,error=traceback.format_exc())),encoding='utf-8')
                def failed():
                    if smoke_root:
                        go.Enabled=True;form.Close();return
                    MessageBox.Show(form,message,'安装未完成' if not uninstall_mode else '卸载未完成');go.Enabled=True;cancel.Enabled=True
                    for box in inputs+browse_buttons:box.Enabled=True
                form.BeginInvoke(Action(failed))
        threading.Thread(target=task,daemon=True).start()
    go.Click+=click
    if smoke_root:
        from System.Windows.Forms import Timer
        timer=Timer();timer.Interval=1500
        def tick(sender,event):
            trace('timer');timer.Stop();form.Show();form.Activate();click(go,None)
        timer.Tick+=tick;timer.Start()
    trace('run');Application.Run(form);trace('closed')

def wizard(uninstall_mode=False,smoke_root=None):
    # Windows controls and folder dialogs require a dedicated STA message loop.
    import clr
    from System.Threading import ApartmentState,Thread,ThreadStart
    errors=[]
    def show():
        try:wizard_window(uninstall_mode,smoke_root)
        except Exception:errors.append(traceback.format_exc())
    thread=Thread(ThreadStart(show));thread.SetApartmentState(ApartmentState.STA);thread.Start()
    while thread.IsAlive:thread.Join(500)
    if errors:raise RuntimeError(errors[0])

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--uninstall',action='store_true');parser.add_argument('--silent',action='store_true');parser.add_argument('--dir');parser.add_argument('--data-dir');parser.add_argument('--test-profile');parser.add_argument('--report');parser.add_argument('--gui-smoke')
    args=parser.parse_args()
    if Path(sys.executable).stem=='卸载量研':args.uninstall=True
    try:
        if args.silent:
            result=uninstall(args.dir) if args.uninstall else install(args.dir or initial_paths()[0],args.data_dir or initial_paths()[1],test_profile=args.test_profile)
            if args.report:Path(args.report).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        else:
            if args.gui_smoke:Path(args.gui_smoke).mkdir(parents=True,exist_ok=True)
            wizard(args.uninstall,args.gui_smoke)
        return 0
    except Exception:
        if args.report:Path(args.report).write_text(json.dumps(dict(passed=False,error=traceback.format_exc()),ensure_ascii=False),encoding='utf-8')
        return 1

if __name__=='__main__':sys.exit(main())
