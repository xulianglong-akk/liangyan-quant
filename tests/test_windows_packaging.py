"""Installer/desktop boundary checks; no registry writes, GUI or production ports."""
import importlib.util
import hashlib
import io
import json
import os
from contextlib import ExitStack,contextmanager
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch,MagicMock
from types import SimpleNamespace
import zipfile

ROOT=Path(__file__).resolve().parents[1]

def source_module(name,path):
    spec=importlib.util.spec_from_file_location(name,path); module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module); return module

INSTALLER=source_module('liangyan_packaging_under_test',ROOT/'packaging'/'installer.py')
DESKTOP=source_module('liangyan_desktop_under_test',ROOT/'src'/'desktop.py')

class PortSocket:
    def __init__(self,busy,attempts): self.busy=busy; self.attempts=attempts
    def __enter__(self): return self
    def __exit__(self,*args): pass
    def bind(self,address):
        self.attempts.append(address)
        if address[1] in self.busy: raise OSError('isolated occupied-port fixture')

class WindowsPackagingBoundaries(unittest.TestCase):
    def setUp(self):
        work=Path('D:/量化数据/work'); work.mkdir(parents=True,exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(prefix='package-unit-',dir=work); self.root=Path(self.tmp.name).resolve()
        self.program=self.root/'Program'; self.data=self.root/'Data'; self.other=self.root/'OtherData'
    def tearDown(self): self.tmp.cleanup()
    def health(self,data=None,**extra):
        return dict(app='quant-assistant',schema='v3',data_root=str(data or self.data),pid=1234,active=True)|extra
    def test_independent_paths_and_unicode_folder_are_accepted(self):
        program=self.root/'量研 软件'; data=self.root/'行情 与账户'
        self.assertEqual(INSTALLER.validate_paths(str(program),str(data)),(program,data))
        self.assertFalse(program.exists()); self.assertFalse(data.exists())
    def test_root_relative_and_nested_paths_are_rejected(self):
        for program,data in [('relative',str(self.data)),(str(self.program),'relative'),('D:/',str(self.data)),(str(self.program),'D:/'),(str(self.program),str(self.program)),(str(self.program),str(self.program/'Data')),(str(self.data/'Program'),str(self.data))]:
            with self.subTest(program=program,data=data),self.assertRaises(ValueError): INSTALLER.validate_paths(program,data)
    def test_unknown_nonempty_program_is_not_overwritten(self):
        self.program.mkdir(); file=self.program/'用户文件.txt'; file.write_text('must be preserved',encoding='utf-8')
        with self.assertRaises(ValueError): INSTALLER.validate_paths(str(self.program),str(self.data))
        self.assertEqual(file.read_text(encoding='utf-8'),'must be preserved')
    def test_only_matching_install_manifest_allows_existing_folder(self):
        self.program.mkdir(); manifest=self.program/INSTALLER.MANIFEST
        manifest.write_text(json.dumps(dict(format='liangyan-install-v1',root=str(self.other))),encoding='utf-8')
        with self.assertRaises(ValueError): INSTALLER.validate_paths(str(self.program),str(self.data))
        manifest.write_text(json.dumps(dict(format='liangyan-install-v1',root=str(self.program))),encoding='utf-8')
        self.assertEqual(INSTALLER.validate_paths(str(self.program),str(self.data)),(self.program,self.data))
    def test_archive_paths_cannot_escape_installation_or_use_ntfs_streams(self):
        self.program.mkdir(); self.data.mkdir(); existing=self.data/'账户.sqlite3'; existing.write_bytes(b'unchanged-user-data')
        unsafe=['../Data/账户.sqlite3','..\\Data\\账户.sqlite3','folder/../../Data/账户.sqlite3','/outside.txt','\\outside.txt','E:\\outside.txt','safe.txt:stream']
        for name in unsafe:
            with self.subTest(name=name),self.assertRaises(ValueError): INSTALLER.safe_member(self.program,name)
        self.assertEqual(INSTALLER.safe_member(self.program,'nested/量研.exe'),self.program/'nested'/'量研.exe')
        self.assertEqual(existing.read_bytes(),b'unchanged-user-data')
    def test_registered_install_locations_are_reused_without_real_registry_read(self):
        values={'InstallDirectory':str(self.program),'DataDirectory':str(self.data)}
        with patch.object(INSTALLER,'registered',side_effect=lambda key,name,default='':values.get(name,default)):
            self.assertEqual(INSTALLER.initial_paths(),(str(self.program),str(self.data)))
    def test_explicit_desktop_data_takes_precedence_over_environment(self):
        with patch.dict(os.environ,{'QUANT_DATA_HOME':str(self.other)},clear=False):
            self.assertEqual(DESKTOP.data_directory(str(self.data)),self.data)
            self.assertEqual(DESKTOP.data_directory(),self.other)
    def test_health_reuse_requires_application_schema_and_exact_data_directory(self):
        accepted=self.health()
        with patch.object(DESKTOP,'request',return_value=accepted): self.assertEqual(DESKTOP.matching_health(8765,self.data),accepted)
        for value in [self.health(self.other),self.health(app='other-application'),self.health(schema='v1'),dict(app='quant-assistant',schema='v3')]:
            with self.subTest(value=value),patch.object(DESKTOP,'request',return_value=value): self.assertIsNone(DESKTOP.matching_health(8765,self.data))
    def test_unfamiliar_or_corrupt_health_payload_is_rejected(self):
        for value in [[],None,'other service',self.health(data_root=None),self.health(data_root=123),self.health(data_root=[])]:
            with self.subTest(value=value),patch.object(DESKTOP,'request',return_value=value): self.assertIsNone(DESKTOP.matching_health(8765,self.data))
    def test_verified_marker_service_is_selected_without_port_mutation(self):
        work=self.data/'work'; work.mkdir(parents=True); (work/'desktop-service.json').write_text(json.dumps(dict(port=8788)),encoding='utf-8')
        accepted=self.health()
        with patch.object(DESKTOP,'request',side_effect=lambda port,*args:accepted if port==8788 else self.health(self.other)),patch.object(DESKTOP.socket,'socket',side_effect=AssertionError('verified reuse must not open a socket')):
            self.assertEqual(DESKTOP.choose_service(self.data,8765),(8788,accepted))
    def test_foreign_marker_and_occupied_service_are_not_reused(self):
        work=self.data/'work'; work.mkdir(parents=True); (work/'desktop-service.json').write_text(json.dumps(dict(port=8788)),encoding='utf-8'); attempts=[]
        with patch.object(DESKTOP,'request',return_value=self.health(self.other)),patch.object(DESKTOP.socket,'socket',side_effect=lambda:PortSocket({8765},attempts)):
            self.assertEqual(DESKTOP.choose_service(self.data),(8766,None))
        self.assertEqual(attempts,[('127.0.0.1',8765),('127.0.0.1',8766)])
    def test_corrupt_marker_falls_back_without_accepting_foreign_data(self):
        work=self.data/'work'; work.mkdir(parents=True); (work/'desktop-service.json').write_text('{broken',encoding='utf-8')
        accepted=self.health()
        for raw in ['{broken','null','[]',json.dumps(dict(port=None)),json.dumps(dict(port='invalid')),json.dumps(dict(port=-1))]:
            (work/'desktop-service.json').write_text(raw,encoding='utf-8')
            with self.subTest(raw=raw),patch.object(DESKTOP,'request',side_effect=lambda port,*args:accepted if port==8767 else self.health(self.other)):
                self.assertEqual(DESKTOP.choose_service(self.data),(8767,accepted))
    def test_all_ports_busy_reports_failure_and_shutdown_sends_nothing_to_foreign_service(self):
        attempts=[]
        with patch.object(DESKTOP,'request',return_value=self.health(self.other)),patch.object(DESKTOP.socket,'socket',side_effect=lambda:PortSocket(set(range(8765,8796)),attempts)):
            with self.assertRaises(RuntimeError): DESKTOP.choose_service(self.data)
        with patch.object(DESKTOP,'choose_service',return_value=(8765,None)),patch.object(DESKTOP,'request') as request,patch.object(DESKTOP,'data_directory',return_value=self.data),patch.object(sys,'argv',['量研.exe','--shutdown']):
            DESKTOP.main_entry(); request.assert_not_called()

    def payload(self):
        assets=self.root/'Assets'; assets.mkdir()
        with zipfile.ZipFile(assets/'application.zip','w') as archive:
            for name in ['量研.exe','卸载量研.exe','_internal/core.dat']: archive.writestr(name,('new:'+name).encode())
        (assets/'WebView2-Standalone-X64.exe').write_bytes(b'never-executed-test-runtime')
        checksums={name:hashlib.sha256((assets/name).read_bytes()).hexdigest() for name in ['application.zip','WebView2-Standalone-X64.exe']}
        (assets/'payload-checksums.json').write_text(json.dumps(checksums),encoding='utf-8'); return assets
    def existing_manifest(self,files=None,data=None):
        self.program.mkdir(exist_ok=True); chosen=files if files is not None else ['量研.exe','卸载量研.exe','_internal/core.dat','legacy.dat']
        for name in chosen:
            file=self.program/name; file.parent.mkdir(parents=True,exist_ok=True); file.write_bytes(('old:'+name).encode())
        value=dict(format='liangyan-install-v1',version='old',root=str(self.program),data=str(data or self.data),files=chosen,shortcuts=[],product_key='mock-product',uninstall_key='mock-uninstall')
        (self.program/INSTALLER.MANIFEST).write_text(json.dumps(value),encoding='utf-8'); return value
    @contextmanager
    def isolated_install(self,assets=None):
        # Even the new registry compatibility branches remain fully mocked.
        with ExitStack() as stack:
            if assets: stack.enter_context(patch.object(INSTALLER,'ASSETS',assets))
            shutdown=stack.enter_context(patch.object(INSTALLER,'shutdown_previous'))
            stack.enter_context(patch.object(INSTALLER,'webview_available',return_value=True))
            stack.enter_context(patch.object(INSTALLER,'shortcut'))
            stack.enter_context(patch.object(INSTALLER,'registered',return_value=''))
            registry={name:stack.enter_context(patch.object(INSTALLER.winreg,name)) for name in ['CreateKey','SetValueEx','DeleteKey','DeleteValue']}
            stack.enter_context(patch.object(INSTALLER.winreg,'OpenKey',side_effect=AssertionError('no real registry reads')))
            stack.enter_context(patch.object(INSTALLER.subprocess,'run',side_effect=AssertionError('no external commands')))
            launch=stack.enter_context(patch.object(INSTALLER.subprocess,'Popen'))
            yield shutdown,registry,launch
    def test_upgrade_preserves_unknown_files_stops_original_data_and_tracks_legacy_ownership(self):
        assets=self.payload(); olddata=self.root/'OldData'; olddata.mkdir(); account=olddata/'account.sqlite3'; account.write_bytes(b'original-account')
        self.existing_manifest(data=olddata); unrelated=self.program/'用户文件.txt'; unrelated.write_bytes(b'preserve')
        with self.isolated_install(assets) as (shutdown,registry,launch):
            manifest=INSTALLER.install(str(self.program),str(self.data),test_profile=str(self.root/'Profile'))
            shutdown.assert_called_once_with(self.program,olddata); launch.assert_not_called()
            self.assertTrue(registry['SetValueEx'].called)
        self.assertEqual(unrelated.read_bytes(),b'preserve'); self.assertEqual(account.read_bytes(),b'original-account')
        self.assertIn('legacy.dat',manifest['files']); self.assertEqual(manifest['data'],str(self.data))
        self.assertEqual((self.program/'量研.exe').read_bytes(),'new:量研.exe'.encode())
    def test_upgrade_rejects_unowned_target_before_shutdown_or_writing(self):
        assets=self.payload(); self.existing_manifest(files=['量研.exe','卸载量研.exe'])
        collision=self.program/'_internal'/'core.dat'; collision.parent.mkdir(); collision.write_bytes(b'unowned-user-file')
        before=(self.program/INSTALLER.MANIFEST).read_bytes()
        with self.isolated_install(assets) as (shutdown,registry,launch):
            with self.assertRaises(ValueError): INSTALLER.install(str(self.program),str(self.data),test_profile=str(self.root/'Profile'))
            shutdown.assert_not_called(); registry['SetValueEx'].assert_not_called()
        self.assertEqual(collision.read_bytes(),b'unowned-user-file'); self.assertEqual((self.program/INSTALLER.MANIFEST).read_bytes(),before)
    def test_atomic_manifest_failure_retains_previous_record_and_cleans_temporary_file(self):
        self.existing_manifest(); path=self.program/INSTALLER.MANIFEST; before=path.read_bytes()
        with patch.object(Path,'replace',side_effect=OSError('isolated interruption before atomic replace')):
            with self.assertRaises(OSError): INSTALLER.write_manifest(path,dict(format='new'))
        self.assertEqual(path.read_bytes(),before); self.assertEqual(list(self.program.glob(INSTALLER.MANIFEST+'.*.tmp')),[])
        INSTALLER.write_manifest(path,dict(format='completed'))
        self.assertEqual(json.loads(path.read_text()),dict(format='completed'))
    def test_uninstall_leaves_data_unknown_files_and_foreign_registry_ownership(self):
        self.data.mkdir(); account=self.data/'account.sqlite3'; account.write_bytes(b'account-preserved')
        self.existing_manifest(); unrelated=self.program/'个人说明.txt'; unrelated.write_bytes(b'keep')
        with self.isolated_install() as (shutdown,registry,launch):
            result=INSTALLER.uninstall(str(self.program)); shutdown.assert_called_once_with(self.program,self.data)
            registry['DeleteKey'].assert_not_called(); registry['DeleteValue'].assert_not_called(); launch.assert_not_called()
        self.assertTrue(result['data_preserved']); self.assertEqual(account.read_bytes(),b'account-preserved'); self.assertEqual(unrelated.read_bytes(),b'keep')
        self.assertFalse((self.program/'量研.exe').exists()); self.assertFalse((self.program/INSTALLER.MANIFEST).exists())
    def test_self_uninstall_defers_only_owned_executable_and_preserves_manifest_until_cleanup(self):
        self.data.mkdir(); self.existing_manifest(); own=self.program/'卸载量研.exe'
        with self.isolated_install() as (shutdown,registry,launch),patch.object(sys,'executable',str(own)):
            INSTALLER.uninstall(str(self.program)); launch.assert_called_once()
            command=launch.call_args.args[0]; self.assertEqual(command[0],'powershell.exe')
            script=command[-1]; self.assertIn('Wait-Process -Id',script); self.assertIn(str(own),script)
            self.assertNotIn('-Recurse',script); self.assertNotIn('Remove-Item -LiteralPath '+"'"+str(self.data)+"'",script)
        self.assertTrue(own.exists()); self.assertTrue((self.program/INSTALLER.MANIFEST).exists()); self.assertTrue(self.data.is_dir())
    def test_shortcut_ownership_rejects_foreign_target_with_same_link_name(self):
        self.program.mkdir(); link=self.root/'量研.lnk'; link.write_bytes(b'link-fixture')
        for target,expected in [(self.program/'量研.exe',True),(self.other/'量研.exe',False),(self.root/'Program-extra'/'量研.exe',False)]:
            with self.subTest(target=target),patch.object(INSTALLER.subprocess,'run',return_value=SimpleNamespace(stdout=str(target).encode('utf-8'))):
                self.assertEqual(INSTALLER.shortcut_owned(link,self.program),expected)
    def test_actual_31_closed_port_probes_finish_without_slow_http_requests(self):
        # Own an ephemeral bound-but-not-listening socket, so no other service
        # can race onto this port and no production port is contacted.
        with DESKTOP.socket.socket() as reserved:
            reserved.bind(('127.0.0.1',0)); port=reserved.getsockname()[1]
            started=time.monotonic()
            with patch.object(DESKTOP.OPENER,'open',side_effect=AssertionError('closed probes must not start HTTP')):
                for _ in range(31): self.assertIsNone(DESKTOP.matching_health(port,self.data))
            self.assertLess(time.monotonic()-started,5.,'closed port scans must use the short TCP deadline')
    def test_listening_health_keeps_full_http_timeout_after_short_tcp_probe(self):
        response=io.StringIO(json.dumps(self.health()))
        with patch.object(DESKTOP.socket,'create_connection',return_value=MagicMock()) as connect,patch.object(DESKTOP.OPENER,'open',return_value=response) as http:
            self.assertEqual(DESKTOP.request(12345),self.health())
        connect.assert_called_once_with(('127.0.0.1',12345),timeout=.08)
        self.assertEqual(http.call_args.kwargs['timeout'],3)

if __name__=='__main__': unittest.main(verbosity=2)
