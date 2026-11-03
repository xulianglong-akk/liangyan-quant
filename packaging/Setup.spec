from pathlib import Path
import os
from PyInstaller.utils.hooks import collect_all

root=Path(SPECPATH).parent
setup=os.environ.get('BUILD_LIANGYAN_SETUP')=='1'
datas=[(str(root/'packaging'/'app.ico'),'.')];binaries=[];hidden=[]
for name in ('clr_loader','pythonnet'):
    d,b,h=collect_all(name);datas+=d;binaries+=b;hidden+=h
if setup:
    folder=Path('E:/AI-Tools/quant-packaging/payload')
    datas += [(str(folder/name),'.') for name in ('application.zip','WebView2-Standalone-X64.exe','payload-checksums.json')]
a=Analysis([str(root/'packaging'/'installer.py')],pathex=[],binaries=binaries,datas=datas,hiddenimports=hidden,excludes=['numpy','pandas','webview','tkinter','PyQt5','PySide6'],noarchive=False)
pyz=PYZ(a.pure)
exe=EXE(pyz,a.scripts,a.binaries,a.datas,[],name='量研安装程序-0.4.0-Windows-x64' if setup else '卸载量研',debug=False,bootloader_ignore_signals=False,strip=False,upx=False,console=False,icon=str(root/'packaging'/'app.ico'),version=str(root/'packaging'/'version.txt'))
