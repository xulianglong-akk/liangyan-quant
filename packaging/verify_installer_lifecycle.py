"""Exercise the built Windows package using only a private profile and folders.

No application source is loaded by this verifier. Every installed-app check runs
the installed .exe with PYTHONHOME/PYTHONPATH cleared; evidence remains on D:.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import socket
import subprocess
import sys
import time
import traceback
from contextlib import closing
from datetime import datetime,timezone,timedelta
import winreg
from urllib.request import Request,build_opener,ProxyHandler

BASE=Path('D:/量化数据/work/packaging').resolve()
PRODUCT='Software\\LiangYan\\LiangYan'
UNINSTALL='Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\LiangYan.Quant'
MANIFEST='liangyan-install.json'

def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()

def read_json(path):return json.loads(path.read_text(encoding='utf-8-sig'))

def registry_values(key):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,key) as handle:
            values={};index=0
            while True:
                try:name,value,kind=winreg.EnumValue(handle,index)
                except OSError:break
                values[name]=dict(value=value,kind=kind);index+=1
            return values
    except FileNotFoundError:return None

def fixture(data):
    folder=data/'data';folder.mkdir(parents=True,exist_ok=True)
    text=folder/'preserved-user-file.txt';text.write_text('Private lifecycle fixture; must survive upgrades and uninstall.\n',encoding='utf-8')
    database=folder/'preserved-fixture.sqlite3'
    with closing(sqlite3.connect(database)) as db:
        db.execute('CREATE TABLE retained(id INTEGER PRIMARY KEY,value TEXT)');db.execute('INSERT INTO retained VALUES(1,?)',('isolated fixture',));db.commit()
    return {str(path):digest(path) for path in (text,database)}

def preserved(values):return all(Path(name).is_file() and digest(Path(name))==value for name,value in values.items())

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--setup',default='E:/AI-Tools/quant-packaging/setup/量研安装程序-0.4.0-Windows-x64.exe');parser.add_argument('--root')
    args=parser.parse_args();setup=Path(args.setup).resolve()
    stamp=datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d-%H%M%S')
    root=Path(args.root).resolve() if args.root else BASE/('lifecycle-final-'+stamp)
    if root==BASE or not root.is_relative_to(BASE) or root.exists():raise ValueError('需要D盘packaging中的全新独立测试文件夹')
    root.mkdir(parents=True);assert root.is_relative_to(BASE)
    program=root/'App';program2=root/'App2';data=root/'Data';newdata=root/'NewData';profile=root/'Profile'
    key_hash=hashlib.sha256(str(profile).encode()).hexdigest()[:16]
    product=PRODUCT+'\\Tests\\'+key_hash;uninstall_key=UNINSTALL+'.Tests.'+key_hash
    before={k:registry_values(k) for k in (PRODUCT,UNINSTALL)}
    result=dict(passed=False,started=datetime.now(timezone(timedelta(hours=8))).isoformat(),root=str(root),setup=str(setup),setup_sha256=digest(setup),test_product_key=product,test_uninstall_key=uninstall_key,steps=[],checks={})
    report=root/'lifecycle-report.json'
    env=os.environ.copy()
    for name in ['PYTHONPATH','PYTHONHOME','QUANT_DATA_HOME','QUANT_DB','QUANT_PORT','QUANT_SERVICE_FILE','QUANT_CONFIG_HOME']:
        env.pop(name,None)
    env.update(PYTHONIOENCODING='utf-8',QUANT_CONFIG_HOME=str(root/'Config'),QUANT_NO_TRAY='1')
    report.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')

    def require(condition,message):
        if not condition:raise AssertionError(message)
    def execute(label,executable,arguments,timeout=300):
        require(executable==setup or executable.resolve().is_relative_to(root),'拒绝执行测试目录之外的应用')
        started=time.monotonic();out=root/(label+'.stdout.log');err=root/(label+'.stderr.log')
        with out.open('wb') as stdout,err.open('wb') as stderr:
            process=subprocess.Popen([str(executable),*map(str,arguments)],cwd=root,env=env,stdout=stdout,stderr=stderr,creationflags=subprocess.CREATE_NO_WINDOW)
            try:code=process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                result['steps'].append(dict(step=label,pid=process.pid,timeout=True,seconds=round(time.monotonic()-started,3)))
                raise RuntimeError(label+'超时；没有强杀进程，请正常退出该测试实例后复核') from None
        step=dict(step=label,pid=process.pid,exit_code=code,seconds=round(time.monotonic()-started,3),stdout=str(out),stderr=str(err));result['steps'].append(step)
        report.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(step,ensure_ascii=True),flush=True)
        require(code==0,label+'失败，检查对应JSON报告及日志')
    def setup_call(label,app,folder):
        destination=root/(label+'.json')
        execute(label,setup,['--silent','--dir',app,'--data-dir',folder,'--test-profile',profile,'--report',destination])
        manifest=read_json(destination)
        require(manifest['product_key']==product and manifest['uninstall_key']==uninstall_key,'安装器没有使用测试注册表命名空间')
        require(Path(manifest['root']).resolve()==app and Path(manifest['data']).resolve()==folder,'安装器路径错配')
        require(all(Path(p).resolve().is_relative_to(profile) for p in manifest['shortcuts']),'快捷方式不在独立测试profile')
        require(all(Path(p).is_file() for p in manifest['shortcuts']),'测试快捷方式未创建')
        return manifest
    def self_test(label,app):
        destination=root/(label+'.json');execute(label,app/'量研.exe',['--self-test',destination],timeout=180)
        value=read_json(destination);require(value.get('passed'),'已安装EXE自检未通过')
        require(Path(value['data']).resolve().is_relative_to(root),'自检使用了测试目录之外的数据')
        require(Path(value['checks']['health']['root']).resolve().is_relative_to(app),'自检后台不是已安装应用资产')
        require(value['checks'].get('frozen_parallel_training')==2,'冻结EXE多进程自检未覆盖')
        return value
    def own_uninstall(label,app,automatic=False):
        arguments=[] if automatic else ['--uninstall']
        destination=root/(label+'.json');execute(label,app/'卸载量研.exe',[*arguments,'--silent','--dir',app,'--test-profile',profile,'--report',destination])
        deadline=time.monotonic()+70
        while ((app/'卸载量研.exe').exists() or (app/MANIFEST).exists()) and time.monotonic()<deadline:time.sleep(.2)
        require(not (app/'卸载量研.exe').exists(),'实际卸载器没有在退出后删除自己')
        require(not (app/MANIFEST).exists(),'卸载清单没有在自删后清理')
        require(not (app/'量研.exe').exists(),'应用主EXE未删除')
        require(registry_values(uninstall_key) is None,'测试卸载登记键仍存在')
        values=registry_values(product) or {};require('InstallDirectory' not in values and 'Version' not in values,'测试产品安装登记未清理')
        return read_json(destination)

    live_process=None;live_port=None
    opener=build_opener(ProxyHandler({}))
    def live_health():
        try:
            with opener.open('http://127.0.0.1:'+str(live_port)+'/api/health',timeout=.7) as response:
                value=json.loads(response.read())
            require(value.get('app')=='quant-assistant' and value.get('schema')=='v3','测试端口服务不是量研 v3')
            require(Path(value['data_root']).resolve()==newdata,'运行中的测试应用使用了其他数据目录')
            require(Path(value['root']).resolve().is_relative_to(program),'测试服务资产不在独立程序目录')
            return value
        except (OSError,ValueError):return None

    try:
        require(registry_values(product) is None and registry_values(uninstall_key) is None,'测试键预先存在，拒绝覆盖')
        manifest=setup_call('01-silent-install',program,data)
        old_fixture=fixture(data);result['checks']['initial_install']=True
        first=self_test('02-installed-exe-self-test',program);result['checks']['installed_exe_without_python_environment']=True
        result['installed_exe_sha256']=digest(program/'量研.exe');result['self_test_reports']=[str(root/'02-installed-exe-self-test.json')]
        keep=program/'keep.txt';require('keep.txt' not in manifest['files'],'keep.txt与发行包文件碰撞')
        keep.write_text('Unknown user-owned program-folder file. Preserve this.\n',encoding='utf-8');keep_hash=digest(keep)
        obsolete=program/'old-owned-file.txt';require('old-owned-file.txt' not in manifest['files'],'旧拥有文件测试名与发行包碰撞')
        obsolete.write_text('Synthetic file owned by an earlier release.\n',encoding='utf-8');obsolete_hash=digest(obsolete)
        manifest['files'].append('old-owned-file.txt');temporary=program/(MANIFEST+'.test.tmp');temporary.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8');temporary.replace(program/MANIFEST)
        upgraded=setup_call('03-upgrade-change-data',program,newdata)
        require(preserved(old_fixture),'升级改数据目录破坏了旧数据')
        require(keep.is_file() and digest(keep)==keep_hash and 'keep.txt' not in upgraded['files'],'升级破坏了未知文件')
        require(obsolete.is_file() and digest(obsolete)==obsolete_hash and 'old-owned-file.txt' in upgraded['files'],'升级丢失旧文件所有权')
        new_fixture=fixture(newdata);result['checks']['upgrade_old_data_unknown_file_old_ownership']=True
        with socket.socket() as listener:
            listener.bind(('127.0.0.1',0));live_port=listener.getsockname()[1]
        with (root/'04-live-app.stdout.log').open('wb') as stdout,(root/'04-live-app.stderr.log').open('wb') as stderr:
            live_process=subprocess.Popen([str(program/'量研.exe'),'--data-dir',str(newdata),'--port',str(live_port),'--smoke-seconds','120'],cwd=root,env=env,stdout=stdout,stderr=stderr,creationflags=subprocess.CREATE_NO_WINDOW)
        ready_by=time.monotonic()+30;health=None
        while live_process.poll() is None and time.monotonic()<ready_by:
            health=live_health()
            if health:break
            time.sleep(.2)
        require(health is not None,'独立应用未在指定测试端口启动')
        result['live_upgrade_before']=dict(launcher_pid=live_process.pid,port=live_port,health=health)
        upgraded=setup_call('04-live-upgrade-normal-exit',program,newdata)
        live_process.wait(timeout=20)
        result['checks']['running_app_upgrade_normal_exit']=live_process.returncode==0
        require(live_process.returncode==0,'运行中应用升级前没有正常退出')
        require(preserved(old_fixture) and preserved(new_fixture),'运行中的升级破坏了测试数据')
        require(keep.is_file() and digest(keep)==keep_hash,'运行中的升级改变了未知文件')
        own_uninstall('05-own-exe-uninstall',program)
        require(preserved(old_fixture) and preserved(new_fixture),'卸载破坏了旧或新数据')
        require(keep.is_file() and digest(keep)==keep_hash,'卸载删除了未知用户文件')
        require(not obsolete.exists(),'旧拥有文件没有被卸载')
        require(not any(Path(p).exists() for p in upgraded['shortcuts']),'自有测试快捷方式没有清理')
        result['checks']['own_exe_self_deleted_data_unknown_preserved']=True
        reinstalled=setup_call('06-reinstall-new-program-same-data',program2,newdata)
        require(preserved(new_fixture),'新目录重装改变了原数据')
        self_test('07-reinstalled-exe-self-test',program2);result['self_test_reports'].append(str(root/'07-reinstalled-exe-self-test.json'))
        own_uninstall('08-reinstalled-own-exe-uninstall',program2,automatic=True)
        require(preserved(old_fixture) and preserved(new_fixture),'第二次卸载破坏数据')
        require(not any(Path(p).exists() for p in reinstalled['shortcuts']),'重装快捷方式残留')
        result['checks']['reinstall_same_data_and_uninstall']=True
        after={k:registry_values(k) for k in (PRODUCT,UNINSTALL)}
        result['production_registration_observation']=dict(unchanged=before==after,note='只读快照；父代理可并行进行用户正式安装，不将其变化归因于测试')
        # Every setup manifest was already checked for the exact private test
        # identity; this verifier never writes the production registry keys.
        result['checks']['private_registration_only']=True
        # Uninstall deliberately preserves DataDirectory for reinstall. Remove
        # only this fresh private Tests leaf now that the complete test is over.
        require(product.startswith(PRODUCT+'\\Tests\\') and uninstall_key.startswith(UNINSTALL+'.Tests.'),'测试注册表键保护失败')
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER,product)
        require(registry_values(product) is None and registry_values(uninstall_key) is None,'测试注册表叶键收尾失败')
        result['checks']['private_registry_keys_cleaned']=True
        result['preserved_files']=old_fixture|new_fixture|{str(keep):keep_hash};result['passed']=True
    except Exception:
        result['error']=traceback.format_exc()
    finally:
        if live_process is not None and live_process.poll() is None:
            try:
                if live_health():
                    req=Request('http://127.0.0.1:'+str(live_port)+'/api/shutdown',data=b'{}',headers={'Content-Type':'application/json','X-Quant-Local':'1'})
                    with opener.open(req,timeout=3) as response:response.read()
                    live_process.wait(timeout=20)
                    result['live_test_cleanup']='正常请求测试应用退出'
            except Exception:result['live_test_cleanup_error']=traceback.format_exc()
        result['finished']=datetime.now(timezone(timedelta(hours=8))).isoformat();result['python_environment_removed']=['PYTHONHOME','PYTHONPATH']
        report.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(dict(passed=result['passed'],report=str(report),checks=result['checks'],error=result.get('error')),ensure_ascii=True),flush=True)
    return 0 if result['passed'] else 1

if __name__=='__main__':sys.exit(main())
