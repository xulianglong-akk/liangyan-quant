"""Pure, causal rules shared by live paper trading and historical replay."""
import math
import re
import statistics
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

TZ = timezone(timedelta(hours=8))
LIMITS = {
    'steady': dict(label='稳健', exposure=.50, weight=.20, risk=.008, slots=3, drawdown=.08),
    'active': dict(label='激进', exposure=.80, weight=.30, risk=.015, slots=4, drawdown=.15),
}
PARAMS = {
    'steady': dict(fast=20, slow=60, momentum=.02, max_vol=.028, atr_multiple=2.5, hold=25, hold_short=15, family='mixed',quality=True,min_amount=50_000_000,max_extension=2.0),
    'active': dict(fast=10, slow=40, momentum=.01, max_vol=.05, atr_multiple=2.0, hold=15, hold_short=8, family='mixed',quality=True,min_amount=30_000_000,max_extension=3.0),
}
COSTS = dict(commission=.00025, minimum=5., transfer=.00001, slippage=.001, participation=.001)

def rounded(value, tick=.01):
    return float((Decimal(str(value))/Decimal(str(tick))).quantize(Decimal('1'), rounding=ROUND_HALF_UP)*Decimal(str(tick)))

def mainboard(code):
    return bool(re.fullmatch(r'(sh\.(600|601|603|605)\d{3}|sz\.(000|001|002|003)\d{3})', code or ''))

def trading_session(clock, calendar):
    day=clock.date().isoformat()
    if day not in calendar: return 'unknown'
    if not calendar[day]: return 'holiday'
    t=clock.strftime('%H:%M:%S')
    if '09:30:00' <= t < '11:30:00' or '13:00:00' <= t < '14:57:00': return 'trading'
    if '11:30:00' <= t < '13:00:00': return 'lunch'
    if '14:57:00' <= t < '15:00:00': return 'auction'
    return 'closed'

def fresh_quote(q, clock):
    try:
        stamp=datetime.fromisoformat(q['source_time']).replace(tzinfo=TZ)
        age=(clock-stamp).total_seconds()
        return stamp.date()==clock.date() and -5<=age<=120 and q['price']>0 and q['volume']>0 and ('high' not in q or q['low']<=q['price']<=q['high'])
    except (ValueError, KeyError, TypeError): return False

def charge(value, side, day, costs=COSTS, kind='stock'):
    tax=(.0005 if day>='2023-08-28' else .001) if side=='sell' and kind=='stock' else 0
    transfer=costs['transfer'] if kind=='stock' else 0
    return rounded(max(costs['minimum'],value*costs['commission'])+value*(transfer+tax))

def limit_band(code, day, st=False):
    if code=='sh.588710': return .20
    if st: return .10 if day>='2026-07-06' else .05
    return .10

def executable(q, side, code, day, costs=COSTS, st=False):
    tick=.001 if code=='sh.588710' else .01
    base=q.get('ask' if side=='buy' else 'bid',0)
    if base<=0 or q.get('preclose',0)<=0 or q.get('volume',0)<=0: return None
    price=rounded(base*(1+costs['slippage'] if side=='buy' else 1-costs['slippage']),tick)
    band=limit_band(code,day,st); up=rounded(q['preclose']*(1+band),tick); down=rounded(q['preclose']*(1-band),tick)
    # No simulated queue priority at a price limit; also reject crossed/invalid quotes.
    if q.get('bid',0)>q.get('ask',0)>0 or not down<price<up: return None
    return price

def adjusted_prices(bars):
    """Prices expressed at the last completed bar's scale, with no future actions."""
    factor=1.; closes=[]; highs=[]; lows=[]
    for i in range(len(bars)-1,-1,-1):
        x=bars[i]; closes.append(x['close']*factor); highs.append(x['high']*factor); lows.append(x['low']*factor)
        if i and bars[i-1]['close']>0: factor*=x['preclose']/bars[i-1]['close']
    return list(reversed(closes)),list(reversed(highs)),list(reversed(lows))

def market_context(benchmark,params,mode):
    if not params.get('quality'): return dict(allow_new=True,exposure_scale=1.,reason='沿用原版市场规则')
    if not benchmark or len(benchmark)<61: return dict(allow_new=False,exposure_scale=0.,reason='沪深300已完成历史不足，暂停新增买入')
    recent=benchmark[-61:]
    if any(not math.isfinite(float(b.get('close',0))) or b.get('close',0)<=0 or b.get('preclose',0)<=0 for b in recent):
        return dict(allow_new=False,exposure_scale=0.,reason='沪深300数据无效，暂停新增买入')
    closes,_,_=adjusted_prices(recent); ma20=statistics.mean(closes[-20:]); ma60=statistics.mean(closes[-60:])
    mom20=closes[-1]/closes[-21]-1; mom60=closes[-1]/closes[-61]-1
    weak=closes[-1]<ma60 or ma20<=ma60 or mom20<=0 or mom60<=0
    if mode=='steady' and weak: return dict(allow_new=False,exposure_scale=0.,reason='沪深300趋势偏弱，稳健模式暂停新增买入')
    scale=.5 if closes[-1]<ma60 and mom20<=0 else (.75 if weak else 1.)
    return dict(allow_new=True,exposure_scale=scale if mode=='active' else 1.,reason='沪深300趋势偏弱，降低新增持仓上限' if mode=='active' and weak else '沪深300趋势通过观察条件')

def signal(bars, params,benchmark=None):
    quality=bool(params.get('quality')); need=max(params['slow'],61 if quality else 22)
    if len(bars)<need: return None
    bs=bars[-need:]; b=bs[-1]
    minimum=params.get('min_amount',20_000_000) if quality else 20_000_000
    if str(b.get('tradestatus'))!='1' or str(b.get('isST'))!='0' or b['amount']<minimum: return None
    if quality and statistics.mean(x['amount'] for x in bs[-20:])<minimum: return None
    closes,highs,lows=adjusted_prices(bs)
    fast=statistics.mean(closes[-params['fast']:]); slow=statistics.mean(closes[-params['slow']:])
    mom=closes[-1]/closes[-21]-1
    vol=statistics.stdev([closes[i]/closes[i-1]-1 for i in range(len(closes)-20,len(closes))])
    atr=statistics.mean([max(highs[i]-lows[i],abs(highs[i]-closes[i-1]),abs(lows[i]-closes[i-1])) for i in range(len(bs)-14,len(bs))])
    if not atr>0 or vol>params['max_vol'] or fast<=slow or mom<params['momentum']: return None
    extra={}; score=mom/(vol+.005)
    if quality:
        mom60=closes[-1]/closes[-61]-1; extension=(closes[-1]-fast)/atr
        if mom60<=0 or extension>params.get('max_extension',2.): return None
        bm=[x for x in benchmark or [] if x['date']<=b['date']]
        if len(bm)<61 or bm[-1]['date']!=b['date']: return None
        bc,_,_=adjusted_prices(bm[-61:]); relative20=mom-(bc[-1]/bc[-21]-1); relative60=mom60-(bc[-1]/bc[-61]-1)
        score=(.4*mom+.25*mom60+.2*relative20+.15*relative60)/(vol+.005)
        extra=dict(momentum60=mom60,relative20=relative20,relative60=relative60,extension_atr=extension)
    families=[]
    if closes[-1]>fast: families.append('trend')
    if closes[-1]>max(highs[-21:-1]): families.append('breakout')
    if slow<closes[-1]<=fast*1.02 and lows[-1]<=fast and closes[-1]>closes[-2]: families.append('pullback')
    family=params['family']
    if not families or family!='mixed' and family not in families: return None
    return dict(close=b['close'],atr=atr,fast=fast,slow=slow,momentum=mom,volatility=vol,score=score,family=family if family!='mixed' else families[-1],asof=b['date'],**extra)

def quantity(price, stop, equity, cash, used, mode, costs=COSTS, volume=float('inf'),exposure_scale=1.):
    limits=LIMITS[mode]
    exposure_scale=float(exposure_scale)
    if not math.isfinite(exposure_scale) or not 0<=exposure_scale<=1: raise ValueError('市场仓位缩放必须在0到1之间')
    budget=max(0,min(cash,equity*limits['weight'],equity*limits['exposure']*exposure_scale-used))
    qty=int(min(budget/price,equity*limits['risk']/max(price-stop,.01),volume*costs['participation'])/100)*100
    while qty>0 and qty*price+charge(qty*price,'buy','2026-01-01',costs)>budget: qty-=100
    exit_price=max(.001,rounded(stop*(1-costs['slippage'])))
    while qty>0 and qty*(price-exit_price)+charge(qty*price,'buy','2026-01-01',costs)+charge(qty*exit_price,'sell','2026-01-01',costs)>equity*limits['risk']: qty-=100
    return max(0,qty)

def holding_period(s,params):
    if not params.get('quality'): return params['hold']
    short=min(params.get('hold_short',params['hold']),params['hold'])
    return short if s.get('family')=='breakout' or s.get('volatility',0)>params['max_vol']*.65 else params['hold']

def make_plan(code,name,bars,params,mode,equity,cash,used,version,costs=COSTS,benchmark=None,exposure_scale=1.):
    bm=[b for b in benchmark or [] if bars and b['date']<=bars[-1]['date']]
    context=market_context(bm,params,mode)
    if not context['allow_new']: return None
    s=signal(bars,params,bm)
    if not s: return None
    c=s['close']; distance=max(c*.025,min(c*.15,s['atr']*params['atr_multiple']))
    lo=rounded(c*.99); hi=rounded(c*1.01); stop=rounded(c-distance); target=rounded(c+2*distance)
    scale=min(exposure_scale,context['exposure_scale'])
    qty=quantity(hi,stop,equity,cash,used,mode,costs,bars[-1]['volume'],exposure_scale=scale)
    family={'trend':'趋势','breakout':'突破','pullback':'上涨趋势中的回撤'}[s['family']]
    reason=f"{family}规则；20日涨幅 {s['momentum']:.1%}，日波动 {s['volatility']:.1%}"
    if params.get('quality'): reason+=f"；60日涨幅 {s['momentum60']:.1%}，20日相对沪深300 {s['relative20']:+.1%}"
    return dict(code=code,name=name,version=version,buy_low=lo,buy_high=hi,stop=stop,target=target,quantity=qty,distance=distance,hold=holding_period(s,params),score=s['score'],signal=s,exposure_scale=scale,market=context,reason=reason,expiry='仅计划日期有效',exit='盘中触及止损或止盈，或达到持有期限；跟踪止损只收紧。当天买入次日才可卖出。')

def select_diverse(plans,history_by_code,mode,held_codes=None):
    """Diversify by measured trailing returns; no sector classification is inferred."""
    threshold=.8 if mode=='steady' else .9; items=[]; exclusions=[]; accepted=list(dict.fromkeys(held_codes or []))
    limit=max(0,min(5,LIMITS[mode]['slots']-len(accepted))); cache={}
    ordered=sorted(plans,key=lambda p:(-p['score'],p['code']))
    for i,p in enumerate(ordered):
        if len(items)>=limit:
            exclusions.extend(dict(code=left['code'],name=left.get('name',left['code']),reason='已达候选数量上限') for left in ordered[i:])
            break
        code=p['code']; cutoff=p.get('signal',{}).get('asof') or p.get('asof')
        def returns(c):
            key=(c,cutoff)
            if key in cache: return cache[key]
            bs=[b for b in history_by_code.get(c,[]) if not cutoff or b['date']<=cutoff]
            value={b['date']:b['close']/b['preclose']-1 for b in bs[-60:] if b.get('preclose',0)>0} if len(bs)>=61 else {}
            cache[key]=value; return value
        left=returns(code); reason=None; detail={}
        if len(left)<60: reason='相关性历史不足'
        for other in accepted if not reason else []:
            right=returns(other); common=sorted(set(left)&set(right))[-60:]
            if len(common)<60: reason='与已有候选或持仓的共同历史不足'; detail=dict(with_code=other); break
            x=[left[d] for d in common]; y=[right[d] for d in common]; mx=statistics.mean(x); my=statistics.mean(y)
            vx=sum((v-mx)**2 for v in x); vy=sum((v-my)**2 for v in y)
            if vx<1e-16 or vy<1e-16: reason='收益相关性暂不能估计'; detail=dict(with_code=other); break
            corr=sum((a-mx)*(b-my) for a,b in zip(x,y))/math.sqrt(vx*vy)
            if corr>threshold: reason='与已有候选或持仓价格变化过于相似'; detail=dict(with_code=other,correlation=round(corr,6)); break
        if reason: exclusions.append(dict(code=code,name=p.get('name',code),reason=reason,**detail))
        else: items.append(p); accepted.append(code)
    return items,exclusions

def parse_action(row):
    """BaoStock operate-year dividends: per-share cash and bonus ratios, not inferred returns."""
    day=row.get('dividOperateDate','')
    if not day: return None
    cash=float(row.get('dividCashPsBeforeTax') or 0)
    bonus=float(row.get('dividStocksPs') or 0)
    reserve=float(row.get('dividReserveToStockPs') or 0)
    pay=row.get('dividPayDate',''); listing=row.get('dividStockMarketDate','')
    # Missing cash payment / bonus listing dates cannot be guessed for executable capital.
    supported=cash>=0 and bonus>=0 and reserve>=0 and (not cash or bool(pay)) and (not bonus+reserve or bool(listing))
    return dict(date=day,cash=cash,bonus=bonus+reserve,pay_date=pay,listing_date=listing,supported=supported,raw=row)

def action_adjust(position, event):
    """Create a receivable; no cash or shares available before official payment/listing.

    Bonus fractions are floored: the missing fraction is a conservative loss,
    never an optimistic credit. The caller records it as a warning.
    """
    if not event['supported']: raise ValueError('公司行动缺少到账/上市日期')
    q=position['quantity']; gross=q*event['cash']; new_shares=q*event['bonus']
    fractional=abs(new_shares-round(new_shares))>.00001
    scale=1+event['bonus']
    for k in ['stop','target','peak','distance']:
        if position.get(k) is not None: position[k]=max(.001,(position[k]-event['cash'])/scale) if k!='distance' else position[k]/scale
    return dict(cash=gross,quantity=int(new_shares),fractional=fractional,price=max(.001,(position['price']-event['cash'])/scale) if position.get('price') else 0,pay_date=event['pay_date'],listing_date=event['listing_date'],code=position['code'])
