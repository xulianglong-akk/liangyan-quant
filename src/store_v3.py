import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from rules_v3 import TZ, PARAMS, LIMITS, COSTS, mainboard, rounded, charge, action_adjust

def now(): return datetime.now(TZ).isoformat(timespec='seconds')
def dump(value): return json.dumps(value,ensure_ascii=False,sort_keys=True,allow_nan=False)

def local_opening():
    # Personal snapshot belongs to the data directory, never to the distributable.
    if getattr(sys,'frozen',False) or os.environ.get('QUANT_EMPTY_REAL')=='1': return {}
    file=Path(os.environ.get('QUANT_DATA_HOME','D:/量化数据'))/'config'/'opening-account.json'
    return json.loads(file.read_text(encoding='utf-8')) if file.is_file() else {}

INITIAL_OPENING=local_opening()
OPENING=INITIAL_OPENING.get('positions',[])

def valid_record_id(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}',value): raise ValueError('记录编号无效，请重新打开录入表单')
    return value

def valid_real_date(value):
    try: parsed=datetime.strptime(value,'%Y-%m-%d').date()
    except (ValueError,TypeError): raise ValueError('请填写有效日期') from None
    if parsed.isoformat()!=value or value<'2026-10-12' or value>datetime.now(TZ).date().isoformat(): raise ValueError('日期须在起始快照与今天之间')
    return value

def finite_number(value,label):
    if isinstance(value,bool): raise ValueError(label+'无效')
    try: value=float(value)
    except (ValueError,TypeError,OverflowError): raise ValueError(label+'无效') from None
    if not math.isfinite(value): raise ValueError(label+'必须是有限数值')
    return value

def ledger_digest(account):
    """Financial state only: quote/stop updates alone must not prevent correction."""
    value={k:account.get(k) for k in ['cash','fees','closed','receivables','applied','invalid','external_cash_changes']}
    value['positions']={c:{k:h.get(k) for k in ['quantity','avg','book_cost','lots','kind','snapshot','date']} for c,h in account['positions'].items()}
    return hashlib.sha256(dump(value).encode()).hexdigest()

class Store:
    def __init__(self,path,legacy=None):
        self.path=Path(path); self.path.parent.mkdir(parents=True,exist_ok=True); self.lock=threading.RLock()
        with self.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS config(key TEXT PRIMARY KEY,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS securities(code TEXT PRIMARY KEY,name TEXT,ipo TEXT,delisted TEXT,active INTEGER,kind TEXT,payload TEXT);
            CREATE TABLE IF NOT EXISTS bars(code TEXT,date TEXT,payload TEXT,fetched TEXT,PRIMARY KEY(code,date));
            CREATE TABLE IF NOT EXISTS calendar(date TEXT PRIMARY KEY,open INTEGER);
            CREATE TABLE IF NOT EXISTS prepared(code TEXT PRIMARY KEY,until TEXT,start TEXT,error TEXT,actions_until TEXT);
            CREATE TABLE IF NOT EXISTS actions(code TEXT,date TEXT,payload TEXT,PRIMARY KEY(code,date));
            CREATE TABLE IF NOT EXISTS openings(id TEXT PRIMARY KEY,date TEXT,payload TEXT);
            CREATE TABLE IF NOT EXISTS accounts(id TEXT PRIMARY KEY,payload TEXT);
            CREATE TABLE IF NOT EXISTS paper_fills(id TEXT PRIMARY KEY,account TEXT,date TEXT,payload TEXT,UNIQUE(account,id));
            CREATE TABLE IF NOT EXISTS real_fills(id TEXT PRIMARY KEY,date TEXT,payload TEXT);
            CREATE TABLE IF NOT EXISTS cash_adjustments(id TEXT PRIMARY KEY,date TEXT,payload TEXT);
            CREATE TABLE IF NOT EXISTS plans(id TEXT PRIMARY KEY,day TEXT,mode TEXT,account TEXT,payload TEXT,UNIQUE(day,mode,account));
            CREATE TABLE IF NOT EXISTS quotes(code TEXT PRIMARY KEY,payload TEXT);
            CREATE TABLE IF NOT EXISTS quote_samples(id TEXT PRIMARY KEY,code TEXT,source_time TEXT,fetched TEXT,payload TEXT);
            CREATE TABLE IF NOT EXISTS notices(key TEXT PRIMARY KEY,time TEXT,payload TEXT);
            CREATE TABLE IF NOT EXISTS equity(account TEXT,date TEXT,payload TEXT,PRIMARY KEY(account,date));
            CREATE TABLE IF NOT EXISTS strategies(id TEXT PRIMARY KEY,mode TEXT,status TEXT,payload TEXT);
            CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,time TEXT,action TEXT,payload TEXT);
            CREATE TABLE IF NOT EXISTS experiments(id TEXT PRIMARY KEY,mode TEXT,created TEXT,payload TEXT);
            ''')
        if self.get('initialized') is None:
            if legacy and Path(legacy).exists():
                with self.db() as db:
                    db.execute('ATTACH DATABASE ? AS legacy',(str(legacy),))
                    db.execute('INSERT OR IGNORE INTO bars SELECT * FROM legacy.bars')
                    db.execute('INSERT OR IGNORE INTO calendar SELECT * FROM legacy.calendar')
            self.set('settings',dict(mode='steady',stage='paper',costs=COSTS,quote_interval=60,source_verified=False,notifications=True))
            self.set('work',dict(active=False,message='点击开始工作',progress=0,status='idle'))
            with self.db() as db:
                snapshot=INITIAL_OPENING|dict(positions=OPENING,note='用户截图导入的起始快照，非历史成交；平均成本按截图原值保存')
                if OPENING: db.execute('INSERT OR IGNORE INTO openings VALUES(?,?,?)',('user-20261012',snapshot['date'],dump(snapshot)))
                account=self.empty_account(INITIAL_OPENING.get('cash',0.))
                for h in OPENING:
                    account['positions'][h['code']]=h|dict(date='2026-10-12',snapshot=True,lots=[dict(date='2026-10-11',quantity=h['quantity'])],book_cost=h['quantity']*h['avg'],stop=None,target=None,peak=h['price'],hold=None)
                account['capital']=INITIAL_OPENING.get('reported_equity',0.); account['peak']=account['capital']
                db.execute('INSERT OR IGNORE INTO accounts VALUES(?,?)',('real',dump(account)))
                for mode,p in PARAMS.items():
                    db.execute('INSERT OR IGNORE INTO accounts VALUES(?,?)',('paper-'+mode,dump(self.empty_account(22000))))
                    value=dict(params=p,created=now(),evidence=None)
                    db.execute('INSERT OR IGNORE INTO strategies VALUES(?,?,?,?)',('v3-initial-'+mode,mode,'current',dump(value)))
            self.set('initialized',now()); self.audit('初始化','独立模拟账户和真实起始快照；旧实验未导入验证')
        # Restart always waits for explicit Start, while retaining all checkpoints.
        self.set('work',self.get('work')|dict(active=False,status='idle',message='已暂停；点击开始工作继续'))
        a=self.account('real')
        for h in a['positions'].values():
            if h.get('snapshot') and not h.get('lots'): h['lots']=[dict(date='2026-10-11',quantity=h['quantity'])]
        self.save_account('real',a)

    @contextmanager
    def db(self):
        con=sqlite3.connect(self.path,timeout=30); con.row_factory=sqlite3.Row
        con.execute('PRAGMA journal_mode=WAL'); con.execute('PRAGMA foreign_keys=ON')
        try:
            with con: yield con
        finally: con.close()

    @staticmethod
    def empty_account(capital):
        return dict(capital=capital,cash=capital,positions={},peak=capital,halt=False,closed=0,days=[],receivables=[],applied=[],invalid=[],warnings=[],fees=0)
    def get(self,key,default=None):
        with self.db() as db: r=db.execute('SELECT payload FROM config WHERE key=?',(key,)).fetchone()
        return json.loads(r[0]) if r else default
    def set(self,key,value):
        with self.db() as db: db.execute('INSERT OR REPLACE INTO config VALUES(?,?)',(key,dump(value)))
    def audit(self,action,value):
        with self.db() as db:
            raw=dump(value)
            # Repeated identical background failures carry no additional evidence.
            if action in ['退市名录待补齐','策略检验未完成','研究采用单进程模式','桌面提醒发送失败']:
                row=db.execute('SELECT time,payload FROM audit WHERE action=? ORDER BY id DESC LIMIT 1',(action,)).fetchone()
                if row and row['payload']==raw and row['time'][:10]==now()[:10]: return
            db.execute('INSERT INTO audit(time,action,payload) VALUES(?,?,?)',(now(),action,raw))
    @staticmethod
    def _revision(db,bump=False):
        row=db.execute("SELECT payload FROM config WHERE key='real_ledger_revision'").fetchone(); value=json.loads(row[0]) if row else 0
        if bump:
            value+=1; db.execute('INSERT OR REPLACE INTO config VALUES(?,?)',('real_ledger_revision',dump(value)))
        return value
    @staticmethod
    def _last_real_date(db):
        return db.execute('SELECT max(date) FROM (SELECT date FROM real_fills UNION ALL SELECT date FROM cash_adjustments)').fetchone()[0]
    def account(self,key):
        with self.db() as db: r=db.execute('SELECT payload FROM accounts WHERE id=?',(key,)).fetchone()
        if not r: raise ValueError('账户不存在')
        return json.loads(r[0])
    def save_account(self,key,value,db=None):
        if db: db.execute('INSERT OR REPLACE INTO accounts VALUES(?,?)',(key,dump(value)))
        else:
            with self.db() as con: self.save_account(key,value,con)
    def bars(self,code,until=None,limit=None):
        sql='SELECT payload FROM bars WHERE code=?'; args=[code]
        if until: sql+=' AND date<=?'; args.append(until)
        sql+=' ORDER BY date DESC'
        if limit: sql+=' LIMIT ?'; args.append(limit)
        with self.db() as db: rows=db.execute(sql,args).fetchall()
        return [json.loads(r[0]) for r in reversed(rows)]
    def calendar(self):
        with self.db() as db: return {r[0]:bool(r[1]) for r in db.execute('SELECT date,open FROM calendar')}
    def strategy(self,mode):
        with self.db() as db: r=db.execute("SELECT * FROM strategies WHERE mode=? AND status='current'",(mode,)).fetchone()
        return dict(r)|dict(payload=json.loads(r['payload']))
    def install_requested_seed(self,release,parameters):
        """One-time authorized starting-rule upgrade; never overwrite learned versions."""
        if self.get('seed_release')==release: return
        changed=[]
        with self.lock,self.db() as db:
            for mode,p in parameters.items():
                current=db.execute("SELECT * FROM strategies WHERE mode=? AND status='current'",(mode,)).fetchone()
                fills=db.execute('SELECT count(*) FROM paper_fills WHERE account=?',('paper-'+mode,)).fetchone()[0]
                if not current or current['id']!='v3-initial-'+mode or fills: continue
                ident=release+'-'+mode; value=dict(params=p,created=now(),evidence=None,forward_evidence=None,note='用户要求增强起始规则；尚未通过收益验证')
                db.execute("UPDATE strategies SET status='retired' WHERE id=?",(current['id'],))
                db.execute('INSERT OR IGNORE INTO strategies VALUES(?,?,?,?)',(ident,mode,'current',dump(value)))
                changed.append(ident)
        self.set('seed_release',release)
        if changed: self.audit('增强起始规则（未验证收益）',changed)
    def equity_value(self,account):
        return account['cash']+sum(h['quantity']*h['price'] for h in account['positions'].values())+sum(x.get('cash',0)+x.get('quantity',0)*x.get('price',0) for x in account['receivables'])
    def fill(self,account_id,side,code,price,qty,day,fees,plan=None,key=None,source_quote=None):
        if not isinstance(qty,int) or isinstance(qty,bool) or qty<=0 or isinstance(price,bool) or not math.isfinite(price) or price<=0 or isinstance(fees,bool) or not math.isfinite(fees) or fees<0: raise ValueError('成交价格、数量和费用无效')
        try: total=price*qty
        except OverflowError: raise ValueError('成交金额过大') from None
        if not math.isfinite(total): raise ValueError('成交金额过大')
        key=key or uuid.uuid4().hex
        is_real=account_id=='real'
        with self.lock,self.db() as db:
            table='real_fills' if is_real else 'paper_fills'
            if db.execute(f'SELECT 1 FROM {table} WHERE id=?',(key,)).fetchone(): return False
            r=db.execute('SELECT payload FROM accounts WHERE id=?',(account_id,)).fetchone()
            if not r: raise ValueError('账户不存在')
            before=json.loads(r[0]) if is_real else None
            a=json.loads(r[0]); h=a['positions'].get(code); value=price*qty
            if side=='buy':
                managed_etf=is_real and (bool(h and h.get('kind')=='etf') or any(x['code']==code and x['kind']=='etf' for x in OPENING))
                if not mainboard(code) and not managed_etf: raise ValueError('新增买入只支持普通沪深主板股票；已有ETF可录入增持')
                if qty%100: raise ValueError('买入数量须为100股整数倍')
                if a['cash']+.005<value+fees: raise ValueError('可用现金不足')
                a['cash']=rounded(a['cash']-value-fees)
                if h:
                    oldq=h['quantity']; cost=h['book_cost']+value+fees
                    h.update(quantity=oldq+qty,book_cost=cost,avg=cost/(oldq+qty),price=price)
                    h.setdefault('lots',[dict(date=h['date'],quantity=oldq)]).append(dict(date=day,quantity=qty))
                else:
                    s=plan or {}; name=s.get('name',code)
                    h=dict(code=code,name=name,quantity=qty,available=0,avg=(value+fees)/qty,book_cost=value+fees,price=price,date=day,lots=[dict(date=day,quantity=qty)],kind='etf' if managed_etf else 'stock',stop=s.get('stop'),target=s.get('target'),distance=s.get('distance'),peak=price,hold=s.get('hold'),plan_id=s.get('id'),age=0)
                    a['positions'][code]=h
            elif side=='sell':
                if not h or qty>h['quantity']: raise ValueError('卖出数量超过持仓')
                if a['cash']+value-fees<-.005: raise ValueError('费用超过卖出金额与可用现金，请核对实际费用')
                available=sum(l['quantity'] for l in h.get('lots',[dict(date=h['date'],quantity=h['quantity'])]) if l['date']<day)
                if qty>available: raise ValueError('当天买入不能当天卖出，或可卖数量不足')
                if qty%100 and qty!=h['quantity']: raise ValueError('零股须一次卖完全部剩余持仓')
                cost=h['book_cost']*qty/h['quantity']; a['cash']=rounded(a['cash']+value-fees)
                if h.get('lots'):
                    left=qty
                    for lot in h['lots']:
                        if lot['date']>=day: continue
                        n=min(left,lot['quantity']); lot['quantity']-=n; left-=n
                    h['lots']=[l for l in h['lots'] if l['quantity']]
                h['quantity']-=qty; h['book_cost']-=cost; h['price']=price
                if h['quantity']==0: del a['positions'][code]; a['closed']+=1
            else: raise ValueError('请选择买入或卖出')
            availability_day=datetime.now(TZ).date().isoformat() if is_real else day
            for holding in a['positions'].values():
                holding['available']=min(holding['quantity'],sum(l['quantity'] for l in holding.get('lots',[]) if l['date']<availability_day))
            a['fees']+=fees; self.save_account(account_id,a,db)
            row=dict(id=key,created=now(),side=side,code=code,date=day,price=price,quantity=qty,fees=fees,plan_id=(plan or {}).get('id'),pnl=rounded(value-fees-cost) if side=='sell' else None)
            if not is_real: row['execution']=dict(source_quote=source_quote,costs=self.get('settings')['costs'],engine='v3',version=(plan or {}).get('version'))
            if is_real:
                row.update(account_before=before,ledger_after=ledger_digest(a),ledger_revision_after=self._revision(db,True),void=False)
                db.execute('INSERT INTO real_fills VALUES(?,?,?)',(key,day,dump(row)))
                db.execute('INSERT INTO audit(time,action,payload) VALUES(?,?,?)',(now(),'录入实际成交',dump(dict(id=key,date=day,code=code,side=side,quantity=qty))))
            else: db.execute('INSERT INTO paper_fills VALUES(?,?,?,?)',(key,account_id,day,dump(row)))
            return row
    def update_marks(self,account_id,quotes,day,mode=None,observe=False):
        with self.lock,self.db() as db:
            a=self.account(account_id)
            for c,h in a['positions'].items():
                q=quotes.get(c)
                if q:
                    h['price']=q['price']; h['peak']=max(h.get('peak',q['price']),q['price'])
                    if h.get('distance') and h.get('stop') is not None: h['stop']=rounded(max(h['stop'],h['peak']-h['distance']),.001 if h['kind']=='etf' else .01)
                h['available']=sum(l['quantity'] for l in h.get('lots',[dict(date=h['date'],quantity=h['quantity'])]) if l['date']<day)
            for r in a['receivables']:
                if r['code'] in quotes: r['price']=quotes[r['code']]['price']
            if observe and day not in a['days']: a['days'].append(day)
            eq=self.equity_value(a); a['peak']=max(a['peak'],eq)
            dd=1-eq/a['peak'] if a['peak'] else 0
            if mode and dd>=LIMITS[mode]['drawdown']: a['halt']=True
            self.save_account(account_id,a,db)
            db.execute('INSERT OR REPLACE INTO equity VALUES(?,?,?)',(account_id,day,dump(dict(date=day,equity=rounded(eq),cash=a['cash'],drawdown=dd))))
            return a
    def apply_actions(self,account_id,day):
        with self.lock:
            a=self.account(account_id)
            previous=ledger_digest(a) if account_id=='real' else None
            for c,h in a['positions'].items():
                with self.db() as db: rows=db.execute('SELECT payload FROM actions WHERE code=? AND date>? AND date<=?',(c,h['date'],day)).fetchall()
                for row in rows:
                    e=json.loads(row[0]); key=c+':'+e['date']
                    if key in a['applied']: continue
                    try:
                        if account_id=='real': raise ValueError('实际分红税及送股到账需对照券商录入')
                        receipt=action_adjust(h,e)
                        # Conservative maximum 20% dividend tax reserve; this is a
                        # modelled cost for evaluation accounts, not an unresolved
                        # event. Real accounts still require broker reconciliation.
                        receipt['cash']*=.8; a['receivables'].append(receipt)
                        if e['cash']:
                            warnings=a.setdefault('warnings',[])
                            note=key+' 现金分红按20%预留税'
                            if note not in warnings and len(warnings)<100: warnings.append(note)
                    except ValueError as ex: a['invalid'].append(key+' '+str(ex))
                    a['applied'].append(key)
            pending=[]
            for r in a['receivables']:
                if r['cash'] and r['pay_date']<=day: a['cash']=rounded(a['cash']+r['cash']); r['cash']=0
                if r['quantity'] and r['listing_date']<=day:
                    h=a['positions'].get(r['code'])
                    if h:
                        h['quantity']+=r['quantity']; h.setdefault('lots',[dict(date=h['date'],quantity=h['quantity']-r['quantity'])]).append(dict(date=r['listing_date'],quantity=r['quantity']))
                        h['avg']=h['book_cost']/h['quantity']
                    else:
                        a['invalid'].append('送股上市前持仓已退出；待核对成本分摊')
                        qty=r['quantity']; a['positions'][r['code']]=dict(code=r['code'],name=r['code'],quantity=qty,available=0,avg=0,book_cost=0,price=r['price'],date=r['listing_date'],lots=[dict(date=r['listing_date'],quantity=qty)],kind='stock',stop=None,target=None,peak=r['price'],hold=None)
                    r['quantity']=0
                if r['cash'] or r['quantity']: pending.append(r)
            a['receivables']=pending
            with self.db() as db:
                self.save_account(account_id,a,db)
                if account_id=='real' and ledger_digest(a)!=previous: self._revision(db,True)
    def real_trade(self,body):
        if not isinstance(body,dict): raise ValueError('成交记录格式无效')
        key=valid_record_id(body.get('id')); day=valid_real_date(body.get('date'))
        side=body.get('side'); code=body.get('code'); qty=body.get('quantity'); price=finite_number(body.get('price'),'成交价格'); fees=finite_number(body.get('fees'),'实际费用')
        if side not in ['buy','sell'] or not isinstance(code,str): raise ValueError('请选择买卖方向并填写股票代码')
        if not isinstance(qty,int) or isinstance(qty,bool) or qty<=0 or price<=0 or fees<0: raise ValueError('成交价格、数量和费用无效')
        expected=dict(date=day,side=side,code=code,price=price,quantity=qty,fees=fees)
        with self.lock:
            with self.db() as db:
                old=db.execute('SELECT payload FROM real_fills WHERE id=?',(key,)).fetchone()
                if old:
                    row=json.loads(old[0])
                    if any(row.get(k)!=v for k,v in expected.items()): raise ValueError('该编号已用于另一笔成交，请核对记录后再保存')
                    return dict(saved=True,already_saved=True,id=key,void=bool(row.get('void')),trade=row)
                last=self._last_real_date(db)
            if last and day<last: raise ValueError('请按实际记录日期顺序录入；该日期早于最近成交或现金核对')
            if not self.calendar().get(day): raise ValueError('该日期不是已确认的交易日，请先开始工作更新日历')
            result=self.fill('real',side,code,price,qty,day,fees,key=key)
            return dict(saved=True,already_saved=False,id=key,void=False,trade=result)
    def real_cash_adjust(self,body,before_change=None):
        if not isinstance(body,dict): raise ValueError('现金核对格式无效')
        cash=finite_number(body.get('cash'),'可用现金'); day=valid_real_date(body.get('date')); reason=body.get('reason','')
        if cash<0: raise ValueError('可用现金不能为负数')
        if not isinstance(reason,str) or not 2<=len(reason.strip())<=500: raise ValueError('请填写2至500字的现金核对原因')
        reason=reason.strip(); expected=dict(cash=rounded(cash),date=day,reason=reason)
        key=valid_record_id(body['id']) if body.get('id') is not None else 'cash-'+hashlib.sha256(dump(expected).encode()).hexdigest()[:32]
        with self.lock,self.db() as db:
            old=db.execute('SELECT payload FROM cash_adjustments WHERE id=?',(key,)).fetchone()
            if old:
                row=json.loads(old[0])
                if any(row.get(k)!=v for k,v in expected.items()): raise ValueError('该编号已用于另一条现金核对，请重新填写')
                return dict(saved=True,already_saved=True,id=key,adjustment=row)
            last=self._last_real_date(db)
            if last and day<last: raise ValueError('现金核对日期不能早于最近真实记录')
            backup=before_change() if before_change else None
            a=json.loads(db.execute("SELECT payload FROM accounts WHERE id='real'").fetchone()[0]); before=a['cash']; delta=rounded(expected['cash']-before)
            a['cash']=expected['cash']; a['external_cash_changes']=rounded(a.get('external_cash_changes',0)+delta)
            self.save_account('real',a,db); revision=self._revision(db,True)
            row=expected|dict(id=key,created=now(),cash_before=before,change=delta,ledger_revision_after=revision,backup=backup,note='手动核对券商可用现金；不代表买卖成交或投资收益')
            db.execute('INSERT INTO cash_adjustments VALUES(?,?,?)',(key,day,dump(row)))
            db.execute('INSERT INTO audit(time,action,payload) VALUES(?,?,?)',(now(),'核对真实可用现金',dump(row)))
            return dict(saved=True,already_saved=False,id=key,adjustment=row)
    def undo_last_real_trade(self,body,before_change=None):
        if not isinstance(body,dict): raise ValueError('更正记录格式无效')
        key=valid_record_id(body.get('id')); reason=body.get('reason','')
        if not isinstance(reason,str) or not 2<=len(reason.strip())<=500: raise ValueError('请填写2至500字的撤销录入原因')
        with self.lock,self.db() as db:
            stored=db.execute('SELECT id,payload FROM real_fills ORDER BY rowid DESC LIMIT 1').fetchone()
            if not stored or stored['id']!=key: raise ValueError('只能撤销最近一次人工成交录入')
            row=json.loads(stored['payload'])
            if row.get('void'): return dict(saved=True,already_saved=True,id=key,void=True)
            if not row.get('account_before'): raise ValueError('该旧记录未保存更正快照，请先备份并人工核对')
            current=json.loads(db.execute("SELECT payload FROM accounts WHERE id='real'").fetchone()[0])
            if row.get('ledger_revision_after')!=self._revision(db) or row.get('ledger_after')!=ledger_digest(current): raise ValueError('之后已有现金核对、权益或其他记账变更，不能直接覆盖；请核对账户')
            backup=before_change() if before_change else None
            restored=row['account_before']
            for code,h in restored['positions'].items():
                observed=current['positions'].get(code)
                if observed:
                    for field in ['price','stop','target','distance','peak','hold','rule_version','observation_start','exit_note']:
                        if field in observed: h[field]=observed[field]
                h['available']=sum(l['quantity'] for l in h.get('lots',[]) if l['date']<datetime.now(TZ).date().isoformat())
            self.save_account('real',restored,db); revision=self._revision(db,True)
            row.update(void=True,voided=now(),void_reason=reason.strip(),void_revision=revision,void_backup=backup)
            db.execute('UPDATE real_fills SET payload=? WHERE id=?',(dump(row),key))
            db.execute('INSERT INTO audit(time,action,payload) VALUES(?,?,?)',(now(),'撤销最近实际成交录入',dump(dict(id=key,reason=reason.strip(),backup=backup,note='仅修改软件账本；没有向券商撤单'))))
            return dict(saved=True,already_saved=False,id=key,void=True)
    def latest_plan(self,account,day=None,mode=None):
        with self.db() as db:
            sql='SELECT payload FROM plans WHERE account=?'; args=[account]
            if day: sql+=' AND day=?'; args.append(day)
            if mode: sql+=' AND mode=?'; args.append(mode)
            r=db.execute(sql+' ORDER BY day DESC LIMIT 1',args).fetchone()
        return json.loads(r[0]) if r else None
    def plan_save(self,account,mode,day,payload):
        payload=dict(payload,day=day,account=account,mode=mode,created=now())
        payload['id']=hashlib.sha256(dump(payload).encode()).hexdigest()[:24]
        for i,p in enumerate(payload['items']): p['id']=payload['id']+':'+str(i)
        with self.db() as db: db.execute('INSERT OR IGNORE INTO plans VALUES(?,?,?,?,?)',(payload['id'],day,mode,account,dump(payload)))
        return self.latest_plan(account,day,mode)
    def notify(self,key,payload):
        with self.db() as db:
            r=db.execute('INSERT OR IGNORE INTO notices VALUES(?,?,?)',(key,now(),dump(payload)))
            return r.rowcount==1
    def coverage(self):
        with self.db() as db:
            alln=db.execute("SELECT count(*) FROM securities WHERE kind='stock'").fetchone()[0]
            eligible=db.execute("SELECT count(*) FROM securities WHERE active=1 AND kind='mainboard'").fetchone()[0]
            # kind mainboard is distinct from other stocks.
            alln+=db.execute("SELECT count(*) FROM securities WHERE kind='mainboard'").fetchone()[0]
            prepared=db.execute("SELECT count(*) FROM prepared p JOIN securities s ON p.code=s.code WHERE s.active=1 AND s.kind='mainboard' AND p.error='' AND p.until=?",(self.get('daily_date',''),)).fetchone()[0]
            bars=db.execute('SELECT count(*) FROM bars').fetchone()[0]
            retired=db.execute("SELECT count(*) FROM securities WHERE active=0 AND kind='mainboard'").fetchone()[0]
            errors=db.execute("SELECT count(*) FROM prepared WHERE error!=''").fetchone()[0]
        return dict(total=alln,eligible=eligible,prepared=prepared,retired=retired,bars=bars,errors=errors,complete=eligible>0 and prepared==eligible,asof=self.get('daily_date'),latest_batch=self.get('daily_count',0))
    def summary(self):
        settings=self.get('settings'); mode=settings['mode']; a=self.account('paper-'+mode)
        plan=self.latest_plan('real' if settings['stage']=='real' else 'paper-'+mode,mode=mode); quotes=self.get_quotes()
        return dict(time=now(),settings=settings,work=self.get('work'),coverage=self.coverage(),plan=plan,quotes=quotes,paper=dict(equity=rounded(self.equity_value(a)),cash=a['cash'],halt=a['halt']),real_cash=self.account('real')['cash'],limits=LIMITS[mode],readiness=self.readiness())
    def get_quotes(self):
        with self.db() as db: return {r[0]:json.loads(r[1]) for r in db.execute('SELECT code,payload FROM quotes')}
    def readiness(self):
        ready=[]
        for mode in PARAMS:
            a=self.account('paper-'+mode); version=self.strategy(mode)['payload']; evidence=version.get('evidence'); forward=version.get('forward_evidence')
            if evidence and evidence.get('passed') and forward and forward.get('passed') and len(a['days'])>=20 and a['closed']>=10 and not a['invalid'] and self.equity_value(a)>a['capital']: ready.append(mode)
        return dict(ready=bool(ready),modes=ready,source_verified=self.get('settings')['source_verified'],message='达到观察门槛，请查看结果' if ready else '尚未完成历史和后续模拟验证')
    def page(self,name):
        with self.db() as db:
            if name=='paper':
                out={}
                for mode in PARAMS:
                    key='paper-'+mode; a=self.account(key)
                    out[mode]=dict(account=a,equity=rounded(self.equity_value(a)),fills=[json.loads(r[0]) for r in db.execute('SELECT payload FROM paper_fills WHERE account=? ORDER BY rowid DESC LIMIT 100',(key,))],curve=[json.loads(r[0]) for r in db.execute('SELECT payload FROM equity WHERE account=? ORDER BY date',(key,))])
                return out
            if name=='real':
                opening=db.execute('SELECT payload FROM openings').fetchone()
                return dict(account=self.account('real'),opening=json.loads(opening[0]) if opening else dict(date=now()[:10],cash=0.,reported_equity=0.,positions=[],note='尚未录入真实账户'),fills=[json.loads(r[0]) for r in db.execute('SELECT payload FROM real_fills ORDER BY rowid DESC LIMIT 100')],cash_adjustments=[json.loads(r[0]) for r in db.execute('SELECT payload FROM cash_adjustments ORDER BY rowid DESC LIMIT 50')],latest_record_date=self._last_real_date(db))
            if name=='evolution': return dict(strategies=[dict(r)|dict(payload=json.loads(r['payload'])) for r in db.execute('SELECT * FROM strategies')],experiments=[dict(r)|dict(payload=json.loads(r['payload'])) for r in db.execute('SELECT * FROM experiments ORDER BY rowid DESC LIMIT 10')],events=[dict(r)|dict(payload=json.loads(r['payload'])) for r in db.execute("SELECT * FROM audit WHERE action LIKE '%晋升%' OR action LIKE '%回退%' OR action LIKE '%候选%' OR action LIKE '%费用%' OR action LIKE '%初始%' OR action LIKE '%起始规则%' OR action LIKE '%后续模拟%' ORDER BY id DESC LIMIT 100")])
            if name=='data': return dict(coverage=self.coverage(),work=self.get('work'),settings=self.get('settings'),errors=[dict(r) for r in db.execute("SELECT * FROM prepared WHERE error!='' LIMIT 30")],metrics=self.get('metrics',{}),quote_health=self.get('quote_health',{}),logs=[dict(r)|dict(payload=json.loads(r['payload'])) for r in db.execute('SELECT * FROM audit ORDER BY id DESC LIMIT 10')],source='BaoStock历史日线 / 新浪公开报价（研究用途）')
        raise ValueError('页面不存在')
