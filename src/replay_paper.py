"""Rebuild a paper ledger from immutable fills and corporate actions, without writing the source."""
import json
import sqlite3
import tempfile
import os
from contextlib import closing
from pathlib import Path
from store_v3 import Store,dump

def reproduce(path,account_id):
    if account_id not in ['paper-steady','paper-active']: raise ValueError('请选择两种主模拟账户之一')
    with closing(sqlite3.connect(path)) as source:
        source.execute('BEGIN')
        original=json.loads(source.execute('SELECT payload FROM accounts WHERE id=?',(account_id,)).fetchone()[0])
        fills=[json.loads(r[0]) for r in source.execute('SELECT payload FROM paper_fills WHERE account=? ORDER BY date,rowid',(account_id,))]
        actions=source.execute('SELECT code,date,payload FROM actions').fetchall()
        plans={r[0]:json.loads(r[1]) for r in source.execute('SELECT id,payload FROM plans WHERE account=?',(account_id,))}
        day=max(original['days']+[f['date'] for f in fills]+['2026-10-12'])
    work=Path(os.environ.get('QUANT_DATA_HOME','D:/量化数据'))/'work'; work.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='ledger-replay-',dir=work) as folder:
        clone=Store(Path(folder)/'replay.sqlite3'); clone.save_account(account_id,clone.empty_account(original['capital']))
        with clone.db() as db: db.executemany('INSERT INTO actions VALUES(?,?,?)',actions)
        for f in fills:
            clone.apply_actions(account_id,f['date'])
            p=None
            if f.get('plan_id'):
                value=plans.get(f['plan_id'].split(':')[0])
                if value: p=next((p for p in value['items'] if p['id']==f['plan_id']),None)
            clone.fill(account_id,f['side'],f['code'],f['price'],f['quantity'],f['date'],f['fees'],p,key=f['id'],source_quote=f.get('execution',{}).get('source_quote'))
        clone.apply_actions(account_id,day); reconstructed=clone.account(account_id)
        def comparable(a): return dict(cash=round(a['cash'],2),fees=round(a['fees'],2),closed=a['closed'],positions={c:dict(quantity=h['quantity'],book_cost=round(h['book_cost'],6)) for c,h in a['positions'].items()},receivables=[{k:r[k] for k in ['code','cash','quantity','pay_date','listing_date']} for r in a['receivables']])
        before=comparable(original); after=comparable(reconstructed)
        return dict(passed=before==after,account=account_id,fills=len(fills),original=before,reconstructed=after,scope='现金、数量、账面成本、费用及权益账本复现；策略触发证据另外保存在原始计划和报价样本中')

if __name__=='__main__':
    import sys
    if len(sys.argv) not in (2,3): raise SystemExit('用法：python src/replay_paper.py paper-steady [数据库路径]')
    value=reproduce(sys.argv[2] if len(sys.argv)==3 else 'D:/量化数据/data/assistant-v3.sqlite3',sys.argv[1]); print(dump(value))
    raise SystemExit(0 if value['passed'] else 1)
