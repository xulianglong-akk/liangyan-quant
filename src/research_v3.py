"""Deterministic historical validation; daily OHLC execution is explicitly conservative."""
import itertools
import math
import sys
from bisect import bisect_left
from rules_v3 import LIMITS, COSTS, PARAMS, charge, quantity, rounded, limit_band, action_adjust,market_context,select_diverse,holding_period

def compiled_signals(bars,p,benchmark=None):
    if not getattr(sys,'frozen',False) and not __import__('importlib.util').util.find_spec('numpy'):
        sys.path.insert(0,'E:/AI-Tools/quant-assistant/python-packages')
    import numpy as np
    n=len(bars)
    quality=bool(p.get('quality')); need=max(p['slow'],61 if quality else 22)
    if n<need: return {}
    c=np.array([b['close'] for b in bars]); pre=np.array([b['preclose'] for b in bars])
    normalized=np.cumprod(c/pre); normalized/=normalized[0]; normalized*=c[0]
    scale=normalized/c; hi=np.array([b['high'] for b in bars])*scale; lo=np.array([b['low'] for b in bars])*scale
    def mean(v,k):
        cum=np.cumsum(np.r_[0.,v]); out=np.full(n,np.nan); out[k-1:]=(cum[k:]-cum[:-k])/k; return out
    fast=mean(normalized,p['fast']); slow=mean(normalized,p['slow'])
    ret=np.r_[0.,normalized[1:]/normalized[:-1]-1]; vmean=mean(ret,20); variance=np.maximum(0,mean(ret*ret,20)-vmean*vmean)*20/19; vol=np.sqrt(variance)
    prev=np.r_[normalized[0],normalized[:-1]]; atr=mean(np.maximum(hi-lo,np.maximum(abs(hi-prev),abs(lo-prev))),14)/scale
    liquidity=mean(np.array([b['amount'] for b in bars]),20) if quality else None
    benchmark_momentum={}
    if quality and benchmark and len(benchmark)>=61:
        bc=np.array([b['close'] for b in benchmark]); bp=np.array([b['preclose'] for b in benchmark])
        valid=np.isfinite(bc)&np.isfinite(bp)&(bc>0)&(bp>0)
        with np.errstate(invalid='ignore',divide='ignore',over='ignore'):
            bn=np.cumprod(np.where(valid,bc/bp,np.nan))
        # A corrupt later row must not suppress already-completed old signals.
        benchmark_momentum={benchmark[j]['date']:(bn[j]/bn[j-20]-1,bn[j]/bn[j-60]-1) for j in range(60,len(benchmark)) if np.isfinite(bn[j])}
    out={}
    for i in range(need-1,n):
        b=bars[i]; mom=normalized[i]/normalized[i-20]-1
        minimum=p.get('min_amount',20_000_000) if quality else 20_000_000
        if b['tradestatus']!='1' or b['isST']!='0' or b['amount']<minimum or not atr[i]>0 or vol[i]>p['max_vol'] or fast[i]<=slow[i] or mom<p['momentum']: continue
        score=float(mom/(vol[i]+.005)); extra={}
        if quality:
            mom60=normalized[i]/normalized[i-60]-1; extension=(normalized[i]-fast[i])/(atr[i]*scale[i])
            bm=benchmark_momentum.get(b['date'])
            if liquidity[i]<minimum or mom60<=0 or extension>p.get('max_extension',2.) or bm is None: continue
            relative20=mom-bm[0]; relative60=mom60-bm[1]
            score=float((.4*mom+.25*mom60+.2*relative20+.15*relative60)/(vol[i]+.005))
            extra=dict(momentum60=float(mom60),relative20=float(relative20),relative60=float(relative60),extension_atr=float(extension))
        f=[]
        if normalized[i]>fast[i]: f.append('trend')
        if normalized[i]>max(hi[i-20:i]): f.append('breakout')
        if slow[i]<normalized[i]<=fast[i]*1.02 and lo[i]<=fast[i] and normalized[i]>normalized[i-1]: f.append('pullback')
        if not f or p['family']!='mixed' and p['family'] not in f: continue
        out[b['date']]=dict(close=float(c[i]),atr=float(atr[i]),fast=float(fast[i]/scale[i]),slow=float(slow[i]/scale[i]),momentum=float(mom),volatility=float(vol[i]),score=score,volume=b['volume'],family=p['family'] if p['family']!='mixed' else f[-1],asof=b['date'],**extra)
    return out

def _evict(cache,tag,limit=2):
    keys=[k for k in cache if isinstance(k,tuple) and k and k[0]==tag]
    while len(keys)>limit: cache.pop(keys.pop(0),None)

def replay(dataset,metadata,actions,params,mode,start,end,costs=None,cancel=lambda:False,benchmark=None,cache=None):
    costs=costs or COSTS
    # A rule set differs from another only in a few parameters. The signal
    # series and the day index do not depend on atr/hold, so 60 grid rules only
    # need a handful of real computations. The caller keeps one cache per
    # dataset; results are unchanged, only repeated work is removed.
    cache=cache if cache is not None else {}
    dkey=id(dataset)
    indexed=cache.get(('indexed',dkey))
    if indexed is None:
        indexed={c:{b['date']:(i,b) for i,b in enumerate(bs)} for c,bs in dataset.items()}
        cache[('indexed',dkey)]=indexed; _evict(cache,'indexed',2)
    quality=bool(params.get('quality'))
    sig_key=('signals',dkey,params.get('fast'),params.get('slow'),params.get('family'),params.get('momentum'),params.get('max_vol'),params.get('max_extension'),params.get('min_amount'),quality,id(benchmark))
    signals=cache.get(sig_key)
    if signals is None:
        signals={}
        for c,bs in dataset.items():
            if cancel(): raise InterruptedError('研究已暂停')
            signals[c]=compiled_signals(bs,params,benchmark=benchmark) if quality else compiled_signals(bs,params)
        cache[sig_key]=signals; _evict(cache,'signals',2)
    bm_days=cache.get(('bm_days',id(benchmark)))
    if bm_days is None:
        bm_days=[b['date'] for b in benchmark or []]; cache[('bm_days',id(benchmark))]=bm_days
    ctx_key=('contexts',id(benchmark),mode,quality)
    contexts=cache.get(ctx_key)
    if contexts is None:
        contexts={b['date']:market_context(benchmark[max(0,i-60):i+1],params,mode) for i,b in enumerate(benchmark or [])} if quality else {}
        cache[ctx_key]=contexts
    days=sorted({b['date'] for bs in dataset.values() for b in bs if start<=b['date']<=end})
    if len(days)<2: raise ValueError('历史区间不足')
    cash=22000.; positions={}; receipts=[]; fills=[]; curve=[]; invalid=[]; peak=22000.; maxdd=0.; halt=False; fees=0.; closed=0
    tax_events=0; fractional_events=0
    def value():
        return cash+sum(h['quantity']*h['price'] for h in positions.values())+sum(r['cash']+r['quantity']*r.get('price',0) for r in receipts)
    def assess():
        nonlocal peak,maxdd,halt
        equity=value(); peak=max(peak,equity); maxdd=max(maxdd,1-equity/peak)
        if 1-equity/peak>=LIMITS[mode]['drawdown']: halt=True
    def sell(c,h,b,i,raw_price,day,reason):
        nonlocal cash,fees,closed
        price=rounded(raw_price*(1-costs['slippage']))
        down=rounded(b['preclose']*(1-limit_band(c,day,b['isST']=='1')))
        previous_volume=dataset[c][i-1]['volume'] if i else 0
        if price<=down or h['quantity']>previous_volume*costs['participation']: return False
        amount=price*h['quantity']; cost=charge(amount,'sell',day,costs); cash+=amount-cost; fees+=cost; closed+=1
        fills.append(dict(date=day,code=c,side='sell',price=price,quantity=h['quantity'],fees=cost,pnl=rounded(amount-cost-h['book_cost']),reason=reason))
        del positions[c]; return True
    previous_day=None
    for day in days:
        if cancel(): raise InterruptedError('研究已暂停')
        for c,h in positions.items():
            if metadata[c].get('delisted') and day>=metadata[c]['delisted']: invalid.append(c+' 退市清算未核实，不能用于升级')
        for c,h in list(positions.items()):
            item=indexed[c].get(day)
            if not item: continue
            i,b=item
            event=actions.get(c,{}).get(day)
            if event or i and abs(b['preclose']-dataset[c][i-1]['close'])>.001:
                try:
                    if not event: raise ValueError('价格除权差异缺少权益记录')
                    if i:
                        reference=(dataset[c][i-1]['close']-event['cash'])/(1+event['bonus'])
                        if abs(b['preclose']-rounded(reference))>.011:
                            invalid.append(c+' '+day+' 权益记录不能解释除权参考价，不能用于升级')
                    receipt=action_adjust(h,event); receipt['cash']*=.8; receipts.append(receipt)
                    if event['cash']: tax_events+=1
                    if receipt.get('fractional'): fractional_events+=1
                except ValueError as ex: invalid.append(c+' '+day+' '+str(ex))
        pending=[]
        for r in receipts:
            if r['cash'] and r['pay_date']<=day: cash+=r['cash']; r['cash']=0
            if r['quantity'] and r['listing_date']<=day:
                    if r['code'] in positions: positions[r['code']]['quantity']+=r['quantity']; r['quantity']=0
                    else: invalid.append('送股未上市前持仓退出；保留未处置权益估值')
            if r['cash'] or r['quantity']: pending.append(r)
        receipts=pending
        # Only information known at the open may fund opening entries. In particular,
        # neither today's close nor cash from a later intraday exit is available yet.
        opening_positions=set(positions)
        for c,h in list(positions.items()):
            item=indexed[c].get(day)
            if not item:
                invalid.append(c+' '+day+' 持仓交易日行情缺失'); continue
            i,b=item; h['age']+=1
            if b['tradestatus']=='1': h['price']=b['open']
            if b['tradestatus']!='1' or day<=h['date']: continue
            price=None; reason=None
            if b['open']<=h['stop']: price=b['open']; reason='止损'
            elif b['open']>=h['target']: price=b['open']; reason='止盈'
            elif h['age']>=h.get('hold',params['hold']): price=b['open']; reason='持有期限'
            if price is not None: sell(c,h,b,i,price,day,reason)
        for r in receipts:
            item=indexed.get(r['code'],{}).get(day)
            if item: r['price']=item[1]['open']
        assess()
        ranked=[]
        prior_index=bisect_left(bm_days,day)-1 if quality else -1
        context=contexts[bm_days[prior_index]] if prior_index>=0 else market_context(None,params,mode)
        if not halt and context['allow_new']:
            for c,bs in dataset.items():
                if c in positions: continue
                m=metadata[c]
                if m.get('ipo') and day<=m['ipo'] or m.get('delisted') and day>=m['delisted']: continue
                item=indexed[c].get(day)
                if not item: continue
                i,b=item
                if not i or b['tradestatus']!='1' or b['isST']!='0': continue
                if previous_day and bs[i-1]['date']!=previous_day: continue
                s=signals[c].get(bs[i-1]['date'])
                if s: ranked.append((s['score'],c,b,s))
        ordered=sorted(ranked,key=lambda row:(-row[0],row[1]))
        if quality:
            proposals=[dict(code=c,name=metadata[c].get('name',c),score=score,signal=s) for score,c,b,s in ordered]
            diverse,_=select_diverse(proposals,dataset,mode,held_codes=list(positions))
            selected={p['code'] for p in diverse}
            ordered=[r for r in ordered if r[1] in selected]
        for _,c,b,s in ordered[:5]:
            if len(positions)>=LIMITS[mode]['slots']: break
            lo=rounded(s['close']*.99); hi=rounded(s['close']*1.01)
            if abs(b['preclose']-s['close'])>.001: continue
            # OHLC lacks ordering/queue data: only next opening entries are credited in validation.
            if not lo<=b['open']<=hi: continue
            price=rounded(b['open']*(1+costs['slippage']))
            if price>=rounded(b['preclose']*(1+limit_band(c,day))) or not lo<=price<=hi: continue
            distance=max(s['close']*.025,min(s['close']*.15,s['atr']*params['atr_multiple']))
            stop=rounded(s['close']-distance); target=rounded(s['close']+2*distance)
            used=sum(h['quantity']*h['price'] for h in positions.values()); equity=cash+used
            qty=quantity(price,stop,equity,cash,used,mode,costs,s['volume'],exposure_scale=context['exposure_scale']) if quality else quantity(price,stop,equity,cash,used,mode,costs,s['volume'])
            if not qty: continue
            cost=charge(price*qty,'buy',day,costs); cash-=price*qty+cost; fees+=cost
            positions[c]=dict(code=c,quantity=qty,date=day,price=price,book_cost=price*qty+cost,stop=stop,target=target,peak=price,distance=distance,age=0,hold=holding_period(s,params))
            fills.append(dict(date=day,code=c,side='buy',price=price,quantity=qty,fees=cost))
        # Intraday exits cannot pay for an earlier opening purchase. On ambiguous
        # daily bars, the adverse stop wins; new opening lots remain subject to T+1.
        for c in opening_positions:
            h=positions.get(c); item=indexed[c].get(day)
            if not h or not item or day<=h['date']: continue
            i,b=item
            if b['tradestatus']!='1': continue
            if b['low']<=h['stop']: sell(c,h,b,i,min(b['open'],h['stop']),day,'止损')
            elif b['high']>=h['target']: sell(c,h,b,i,max(b['open'],h['target']),day,'止盈')
        for c,h in positions.items():
            if day in indexed[c]:
                b=indexed[c][day][1]; h['price']=b['close']
                if b['tradestatus']=='1':
                    h['peak']=max(h['peak'],b['high']); h['stop']=max(h['stop'],h['peak']-h['distance'])
        for r in receipts:
            if day in indexed.get(r['code'],{}): r['price']=indexed[r['code']][day][1]['close']
        assess(); equity=value()
        curve.append(dict(date=day,equity=rounded(equity),cash=rounded(cash)))
        previous_day=day
    if receipts: invalid.append('验证区间结束仍有未完成权益到账/上市，不能用于升级')
    bm=[b for b in benchmark or [] if start<=b['date']<=end]
    warnings=[]
    if tax_events: warnings.append(f'现金分红 {tax_events} 笔按20%预留税，结果偏保守')
    if fractional_events: warnings.append(f'零碎送股 {fractional_events} 笔向下取整，结果偏保守')
    return dict(metrics=dict(total_return=curve[-1]['equity']/22000-1,max_drawdown=maxdd,closed=closed,fees=rounded(fees),cash_return=0,benchmark_return=bm[-1]['close']/bm[0]['close']-1 if len(bm)>1 else None),fills=fills,curve=curve,positions=positions,invalid=sorted(set(invalid)),warnings=warnings,assumptions=['历史入场只计下一交易日开盘区间，盘中触价留给后续真实报价模拟验证','开盘交易只使用已知开盘信息；盘中退出现金不用于当天开盘买入','日线同日同时触及止损止盈时按止损；不推断盘中先后','滑点为成交价格的成本调整，不代表具有历史盘口成交保证','现金分红按20%预留税、零碎送股向下取整属保守处理；未完成权益及退市清算处理的样本不能用于升级；价格指数基准不含分红'])

def historical_gate(candidate,baseline,stress,mode):
    c=candidate['metrics']; b=baseline['metrics']; s=stress['metrics']
    warnings=sorted(set(candidate.get('warnings',[])+baseline.get('warnings',[])+stress.get('warnings',[])))
    reasons=[]
    if any(not isinstance(m.get(k),(int,float)) or not math.isfinite(m[k]) for m in (c,b,s) for k in ('total_return','max_drawdown','closed')):
        return dict(passed=False,reasons=['历史收益或风险指标无效'],candidate=c,baseline=b,stress=s,warnings=warnings)
    if candidate['invalid'] or baseline['invalid'] or stress['invalid']: reasons.append('权益或历史样本存在未解决事件')
    if c['total_return']<=max(0,b['total_return']): reasons.append('扣费收益未超过当前版本和空仓')
    if c['max_drawdown']>min(LIMITS[mode]['drawdown'],b['max_drawdown']+.005): reasons.append('亏损幅度未达门槛')
    if s['total_return']<=0: reasons.append('双倍费用压力下未盈利')
    if c['closed']<10: reasons.append('历史完成交易不足10笔')
    if c['benchmark_return'] is None or not math.isfinite(c['benchmark_return']): reasons.append('缺少有效指数基准')
    return dict(passed=not reasons,reasons=reasons,candidate=c,baseline=b,stress=s,warnings=warnings)

def candidate_grid(mode):
    base=PARAMS[mode]; holds=[10,15,20,25,30] if mode=='steady' else [5,10,15,20,25]
    return [base|dict(family=f,momentum=m,atr_multiple=a,hold=h) for f,m,a,h in itertools.product(['trend','breakout','pullback'],[.01,.03],[2.,3.],holds)][:60]

def forward_gate(challenger,current,limits):
    if challenger['invalid'] or current['invalid'] or challenger['halt']: return 'failed'
    if challenger['receivables'] or current['receivables']: return 'waiting'
    if len(set(challenger['days'])&set(current['days']))<20 or challenger['closed']<10: return 'waiting'
    def eq(a): return a['cash']+sum(h['quantity']*h['price'] for h in a['positions'].values())+sum(r['cash']+r['quantity']*r.get('price',0) for r in a['receivables'])
    if not all(math.isfinite(x) and x>0 for x in (challenger['capital'],current['capital'],challenger['peak'])): return 'failed'
    if not math.isfinite(eq(challenger)) or not math.isfinite(eq(current)): return 'failed'
    if eq(challenger)<=challenger['capital']: return 'failed'
    if eq(challenger)/challenger['capital']<=eq(current)/current['capital']: return 'failed'
    if 1-eq(challenger)/challenger['peak']>limits['drawdown']: return 'failed'
    return 'passed'
