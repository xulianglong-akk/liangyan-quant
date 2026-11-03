from pathlib import Path
import json,hashlib,zipfile,shutil
tools=Path('E:/AI-Tools/quant-packaging');payload=tools/'payload';payload.mkdir(exist_ok=True)
root=tools/'dist'/'LiangYan'
with zipfile.ZipFile(payload/'application.zip','w',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
    for file in root.rglob('*'):
        if file.is_file():
            if file.suffix in ('.sqlite3','.log') or file.name=='opening-account.json':raise ValueError('发现用户数据，拒绝打包')
            z.write(file,file.relative_to(root).as_posix())
shutil.copyfile(tools/'downloads'/'WebView2-Standalone-X64.exe',payload/'WebView2-Standalone-X64.exe')
values={name:hashlib.sha256((payload/name).read_bytes()).hexdigest() for name in ('application.zip','WebView2-Standalone-X64.exe')}
(payload/'payload-checksums.json').write_text(json.dumps(values,indent=2),encoding='utf-8')
print('安装内容准备完成，不包含个人账户与行情数据库')
