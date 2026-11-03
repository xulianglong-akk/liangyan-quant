"""Replay a saved experiment using its immutable local data snapshot."""
import json
import sys
from pathlib import Path
from engine import backtest

if len(sys.argv)!=2: raise SystemExit('用法：python src/replay.py outputs/backtest-<id>.json')
path=Path(sys.argv[1]).resolve(); saved=json.loads(path.read_text(encoding='utf-8')); m=saved['manifest']
snapshot=path.parent/m['snapshot']; data=json.loads(snapshot.read_text(encoding='utf-8'))
import hashlib
if hashlib.sha256(snapshot.read_bytes()).hexdigest()!=m['data_sha256']: raise SystemExit('数据快照哈希不匹配，拒绝复现')
result=backtest(data,m['universe'],m['params'],saved['start'],saved['end'],m['capital'],m['costs'],benchmark=m['benchmark'])
if result['metrics']!=saved['metrics'] or result['trades']!=saved['trades'] or result['curve']!=saved['curve']: raise SystemExit('复现结果不一致')
print('PASS：资金曲线、指标、逐笔交易完全一致')
