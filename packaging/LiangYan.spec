from pathlib import Path
from PyInstaller.utils.hooks import collect_all,collect_submodules,copy_metadata

root=Path(SPECPATH).parent
datas=[(str(root/'web'),'web'),(str(root/'packaging'/'app.ico'),'.'),(str(root/'packaging'/'guide.docx'),'.')]
datas += [(str(root/'src'/name),'.') for name in ('runtime_v3.py','rules_v3.py','research_v3.py')]
binaries=[]; hidden=['desktop_selftest','numpy','pandas','baostock','webview.platforms.edgechromium','webview.platforms.winforms']
for name in ('webview','baostock','clr_loader','pythonnet','tzdata'):
    d,b,h=collect_all(name);datas+=d;binaries+=b;hidden+=h
for name in ('numpy','pandas','pywebview','pythonnet','baostock'):
    datas+=copy_metadata(name)
a=Analysis([str(root/'src'/'desktop.py')],pathex=[str(root/'src')],binaries=binaries,datas=datas,hiddenimports=hidden,hookspath=[],hooksconfig={},runtime_hooks=[],excludes=['PyQt5','PyQt6','PySide2','PySide6','matplotlib','scipy','tkinter','IPython','pytest'],noarchive=False)
pyz=PYZ(a.pure)
exe=EXE(pyz,a.scripts,[],exclude_binaries=True,name='量研',debug=False,bootloader_ignore_signals=False,strip=False,upx=False,console=False,disable_windowed_traceback=False,icon=str(root/'packaging'/'app.ico'),version=str(root/'packaging'/'version.txt'))
coll=COLLECT(exe,a.binaries,a.datas,strip=False,upx=False,name='LiangYan')
