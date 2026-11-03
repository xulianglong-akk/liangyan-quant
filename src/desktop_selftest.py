"""Self-contained frozen build validation, always confined to a fresh data folder."""
import json
import os
from pathlib import Path
import socket
import threading
import time
import traceback

def run(report):
    report=report.resolve(); report.parent.mkdir(parents=True,exist_ok=True)
    folder=report.parent/('frozen-check-'+str(os.getpid()))
    if folder.exists(): raise RuntimeError('验证目录已经存在，拒绝覆盖')
    folder.mkdir()
    with socket.socket() as sock: sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
    os.environ.update(QUANT_DATA_HOME=str(folder),QUANT_PORT=str(port),QUANT_EMPTY_REAL='1',QUANT_NO_TRAY='1')
    os.environ.pop('QUANT_DB',None)
    from desktop import setup_logging,request,matching_health
    setup_logging(folder)
    import main as service
    from rules_v3 import PARAMS
    from market_v3 import BaoSource,sina_quotes,validate_bars
    from research_v3 import compiled_signals
    from datetime import date,timedelta
    import multiprocessing as mp
    ready=threading.Event(); errors=[]
    def start():
        try: service.main(on_ready=ready.set)
        except Exception: errors.append(traceback.format_exc()); ready.set()
    thread=threading.Thread(target=start,daemon=True); thread.start()
    result=dict(version='0.4.0',data=str(folder),port=port,checks={},errors=[])
    try:
        assert ready.wait(20) and not errors,errors
        result['checks']['health']=matching_health(port,folder)
        assert result['checks']['health']
        real=request(port,'page?name=real'); paper=request(port,'page?name=paper')
        assert real['account']['cash']==0 and not real['account']['positions']
        assert all(paper[m]['account']['cash']==22000 for m in PARAMS)
        result['checks']['fresh_accounts']=True
        assert request(port,'state')['settings']['stage']=='paper'
        import urllib.request
        op=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with op.open(f'http://127.0.0.1:{port}/',timeout=3) as page: assert b'v3.js' in page.read()
        with op.open(f'http://127.0.0.1:{port}/api/tutorial',timeout=3) as guide: assert guide.read(2)==b'PK'
        result['checks']['web_and_tutorial']=True
        # Exercise the actual frozen process-spawn adapter with a small real request.
        values=BaoSource().call('history',dict(code='sh.600000',start='2026-10-08',end='2026-10-12',actions=False),timeout=60)
        bars=validate_bars(values['bars'],'sh.600000'); result['checks']['frozen_baostock_process']=len(bars)
        q=sina_quotes(['sh.600000']); assert q; result['checks']['direct_quotes']=len(q)
        # Pure vector execution and nested ProcessPoolExecutor in the frozen executable.
        history=[];day=date(2024,1,2);price=10.
        while len(history)<400:
            if day.weekday()<5:
                prior=price;price*=1.0008
                history.append(dict(code='sh.600000',date=day.isoformat(),open=prior,high=price*1.01,low=prior*.99,close=price,preclose=prior,volume=10000000.,amount=100000000.,tradestatus='1',isST='0'))
            day+=timedelta(days=1)
        compiled_signals(history,PARAMS['steady'],history)
        from runtime_v3 import training_initializer,training_trial
        from concurrent.futures import ProcessPoolExecutor
        snapshot=folder/'training.json'
        snapshot.write_text(json.dumps(dict(dataset={'sh.600000':history},metadata={'sh.600000':dict(ipo='2000-01-01',delisted='',active=1,kind='mainboard')},actions={},benchmark=history)),encoding='utf-8')
        context=mp.get_context('spawn'); event=context.Event()
        with ProcessPoolExecutor(max_workers=2,mp_context=context,initializer=training_initializer,initargs=(str(snapshot),event,history[-1]['date'],service.STORE.get('settings')['costs'])) as pool:
            jobs=[pool.submit(training_trial,i,PARAMS['steady'],'steady',(history[80]['date'],history[-1]['date'])) for i in range(2)]
            outputs=[job.result(timeout=45) for job in jobs]
        assert {value[0] for value in outputs}=={0,1}; result['checks']['frozen_parallel_training']=2
        result['passed']=True
    except Exception:
        result['passed']=False;result['errors'].append(traceback.format_exc())
    finally:
        if service.RUNTIME: service.RUNTIME.stop();service.RUNTIME.shutdown.set()
        if service.SERVER: service.SERVER.shutdown()
        thread.join(5)
        report.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return 0 if result['passed'] else 1
