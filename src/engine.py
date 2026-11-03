"""Deterministic daily-bar research. No broker integration or AI service."""
import math
import statistics
from decimal import Decimal, ROUND_HALF_UP

MODES = {
    'steady': dict(label='稳健', fast=20, slow=60, momentum=0.02, max_vol=0.028, stop=0.06, target=0.12, trail=0.07, hold=25, weight=0.18, exposure=0.55, risk=0.008, slots=3),
    'active': dict(label='激进', fast=10, slow=40, momentum=0.01, max_vol=0.05, stop=0.09, target=0.20, trail=0.10, hold=15, weight=0.28, exposure=0.85, risk=0.015, slots=4),
}
DEFAULT_COSTS = dict(commission=0.00025, minimum=5.0, transfer=0.00001, slippage=0.001, participation=0.001)

def money(x):
    return float(Decimal(str(x)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))

def corporate_actions(bars):
    return [b['date'] for a, b in zip(bars, bars[1:]) if a['close'] > 0 and abs(b['preclose']-a['close']) > 0.001]

def features(bars, p):
    if len(bars) < max(p['slow'], 22):
        return None
    # Use only actions known by this signal date; align past prices to current scale.
    closes = [b['close'] for b in bars[-max(p['slow'],22):]]
    subset = bars[-len(closes):]
    factor = 1.0
    for j in range(len(closes)-1, -1, -1):
        closes[j] *= factor
        if j and subset[j-1]['close']:
            factor *= subset[j]['preclose']/subset[j-1]['close']
    fast = statistics.mean(closes[-p['fast']:])
    slow = statistics.mean(closes[-p['slow']:])
    returns = [closes[j]/closes[j-1]-1 for j in range(len(closes)-20,len(closes))]
    vol = statistics.stdev(returns)
    mom = closes[-1]/closes[-21]-1
    b = bars[-1]
    eligible = b['tradestatus'] == '1' and b['isST'] == '0' and b['volume'] > 0 and b['amount'] >= 20_000_000
    signal = eligible and b['close'] > fast > slow and mom >= p['momentum'] and vol <= p['max_vol']
    return dict(fast=fast, slow=slow, momentum=mom, volatility=vol, signal=signal, score=mom/(vol+0.005), close=b['close'], date=b['date'])

def plan(bars, p, capital, version, code, name):
    f = features(bars,p)
    if not f or not f['signal']:
        return None
    c = f['close']; lo=money(c*0.985); hi=money(c*1.015); stop=money(c*(1-p['stop']))
    risk_fraction=(hi-stop)/hi
    budget=min(capital*p['weight'], capital*p['risk']/risk_fraction)
    qty=max(0,int(min(budget/(hi*1.002),capital*p['risk']/(hi-stop))/100)*100)
    return dict(code=code,name=name,date=f['date'],version=version,score=round(f['score'],2),features=f,buy_low=lo,buy_high=hi,stop=stop,target=money(c*(1+p['target'])),trail=p['trail'],hold=p['hold'],quantity=qty,budget=round(budget,2),reason=f"收盘价 > MA{p['fast']} > MA{p['slow']}；20日动量 {f['momentum']:.1%}；日波动 {f['volatility']:.1%}",entry='下一交易日开盘价处于买入区间，非停牌、非风险警示，未触及涨停；核对最新行情后手动执行',invalid='仅下一交易日有效；开盘跳出区间、数据过期或交易受限时失效',exit='收盘触及止损 / 止盈 / 从持有期最高收盘回撤达到跟踪阈值 / 超过持有期；下一交易日尝试卖出')

def fee(value, side, date, costs):
    tax = (0.0005 if date >= '2023-08-28' else 0.001) if side == 'sell' else 0
    transfer = costs['transfer'] if date >= '2022-04-29' else 0.00002
    return max(costs['minimum'],value*costs['commission']) + value*(transfer+tax)

def can_fill(bar, side, slip):
    if not bar or bar['tradestatus'] != '1' or bar['isST'] != '0' or bar['volume'] <= 0:
        return False
    up=money(bar['preclose']*1.10); down=money(bar['preclose']*0.90)
    price=money(bar['open']*(1+slip if side=='buy' else 1-slip))
    # Conservative: no assumed queue priority at either relevant limit.
    return bar['open'] < up and price <= bar['high'] and price < up if side=='buy' else bar['open'] > down and price >= bar['low'] and price > down

def backtest(dataset, names, p, start, end, capital=100000, costs=None, cancel=lambda:False, benchmark=None):
    if start > end: raise ValueError('开始日期不能晚于结束日期')
    costs=costs or DEFAULT_COSTS
    # Fail closed: raw-price execution cannot silently ignore distributions/splits.
    actions={c:[d for d in corporate_actions(bs) if start<=d<=end] for c,bs in dataset.items()}
    actions={c:d for c,d in actions.items() if d}
    if actions:
        desc='；'.join(f"{names.get(c,c)} {','.join(d)}" for c,d in actions.items())
        raise ValueError('区间存在除权/公司行动，本版尚未实现权益入账，已阻止失真回测。请缩短区间或从观察池移除相关股票：'+desc)
    days=sorted({b['date'] for bars in dataset.values() for b in bars if start<=b['date']<=end})
    if len(days)<2: raise ValueError('区间内有效交易日不足，请先更新数据')
    indexed={c:{b['date']:(i,b) for i,b in enumerate(bs)} for c,bs in dataset.items()}
    cash=float(capital); positions={}; curve=[]; trades=[]; fills=[]; peak=capital; dd=0; total_fees=0; turnover=0; pending_exits={}; prices={}; skipped=0
    for day_index,day in enumerate(days):
        if cancel(): raise InterruptedError('任务已取消')
        for c in list(positions):
            pos=positions[c]; item=indexed[c].get(day)
            if not item: continue
            i,b=item
            if c in pending_exits and day > pos['date'] and can_fill(b,'sell',costs['slippage']):
                q=pos['qty']
                prev_volume=dataset[c][i-1]['volume'] if i else 0
                if q>prev_volume*costs['participation']: continue
                price=money(b['open']*(1-costs['slippage'])); value=price*q; charge=fee(value,'sell',day,costs)
                cash+=value-charge; total_fees+=charge; turnover+=value
                fills.append(dict(code=c,name=names.get(c,c),date=day,side='sell',price=price,quantity=q,fees=money(charge)))
                trades.append(dict(code=c,name=names.get(c,c),buy_date=pos['date'],sell_date=day,buy_price=pos['price'],sell_price=price,quantity=q,fees=money(pos['fees']+charge),pnl=money(value-charge-pos['cost']),return_pct=(value-charge)/pos['cost']-1,holding=pos['age'],reason=pending_exits.pop(c)))
                del positions[c]
        ranked=[]
        for c,bs in dataset.items():
            item=indexed[c].get(day)
            if not item: continue
            i,b=item
            # The signal uses the previous completed bar, never today's OHLC.
            if i==0 or c in positions: continue
            if bs[i-1]['date'] >= day: raise ValueError('日期顺序异常')
            if day_index and bs[i-1]['date'] != days[day_index-1]: continue
            f=features(bs[:i],p)
            if f and f['signal']: ranked.append((f['score'],c,b,f,bs[i-1]['volume']))
        for _,c,b,f,prev_volume in sorted(ranked,reverse=True):
            if len(positions)>=p['slots']: break
            if not f['close']*.985<=b['open']<=f['close']*1.015 or not can_fill(b,'buy',costs['slippage']): skipped+=1; continue
            equity=cash+sum(pos['qty']*prices.get(k,pos['price']) for k,pos in positions.items())
            used=equity-cash
            budget=min(equity*p['weight'],equity*p['risk']/p['stop'],equity*p['exposure']-used,cash)
            price=money(b['open']*(1+costs['slippage'])); qty=max(0,int(budget/(price*1.002)/100)*100)
            qty=min(qty,int(prev_volume*costs['participation']/100)*100)
            if qty<=0: continue
            charge=fee(price*qty,'buy',day,costs)
            while qty>0 and price*qty+charge>min(cash,budget): qty-=100; charge=fee(price*qty,'buy',day,costs)
            if qty<=0: continue
            value=price*qty; cash-=value+charge; total_fees+=charge; turnover+=value
            fills.append(dict(code=c,name=names.get(c,c),date=day,side='buy',price=price,quantity=qty,fees=money(charge)))
            positions[c]=dict(date=day,price=price,qty=qty,cost=value+charge,fees=charge,age=0,peak=price)
        for c,pos in positions.items():
            item=indexed[c].get(day)
            if not item: continue
            _,b=item; prices[c]=b['close']; pos['age']+=1; pos['peak']=max(pos['peak'],b['close'])
            if b['close']<=pos['price']*(1-p['stop']): pending_exits[c]='收盘止损'
            elif b['close']>=pos['price']*(1+p['target']): pending_exits[c]='收盘止盈'
            elif b['close']<=pos['peak']*(1-p['trail']): pending_exits[c]='跟踪止损'
            elif pos['age']>=p['hold']: pending_exits[c]='最长持有期'
        equity=cash+sum(pos['qty']*prices.get(c,pos['price']) for c,pos in positions.items()); peak=max(peak,equity); dd=max(dd,1-equity/peak)
        curve.append(dict(date=day,equity=money(equity),cash=money(cash)))
    returns=[curve[i]['equity']/curve[i-1]['equity']-1 for i in range(1,len(curve))]
    sigma=statistics.stdev(returns) if len(returns)>1 else 0
    sharpe=statistics.mean(returns)/sigma*math.sqrt(252) if sigma else 0
    bm=[b for b in (benchmark or []) if start<=b['date']<=end]
    bm_return=bm[-1]['close']/bm[0]['close']-1 if len(bm)>1 else None
    for row in curve:
        bar=next((b for b in bm if b['date']==row['date']),None)
        row['benchmark']=money(capital*bar['close']/bm[0]['close']) if bm and bar else None
    result=dict(start=start,end=end,metrics=dict(total_return=curve[-1]['equity']/capital-1,max_drawdown=dd,sharpe=sharpe,trades=len(trades),fees=money(total_fees),turnover=turnover/capital,avg_holding=statistics.mean([t['holding'] for t in trades]) if trades else 0,win_rate=sum(t['pnl']>0 for t in trades)/len(trades) if trades else 0,benchmark_return=bm_return,skipped=skipped),curve=curve,trades=trades,open_positions=positions,notes=['期末持仓按收盘估值，未强制卖出；未计未成交卖出费用','仅用户观察池，存在幸存者与选择偏差，不能外推至全市场','日线收盘退出、下一交易日开盘执行；不模拟盘中触价','公司行动跨期回测被阻止；价格指数基准不含分红及费用','成交量约束使用前一日成交量；开盘流动性无法由日线证实，属于保守代理假设'])
    result['fills']=fills
    return result
