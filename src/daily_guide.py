"""Plain-language summaries of existing rules; no invented quotes or signals."""

def describe_error(message):
    message=message or ''
    if '公司行动' in message or '除权' in message:
        return dict(title='这段时间里有股票分红或送股，暂时不能准确算收益。',next='把回测开始日期改到提示日期之后，再试一次。')
    if '至少250' in message or '样本' in message:
        return dict(title='可用的完整历史还不够，暂时不能自动改进选股方法。',next='现在仍可以查看股票和记录买卖；原来的方法会保留。')
    if '冻结' in message:
        return dict(title='你已暂停修改选股方法。',next='需要继续时，点“解除冻结”。')
    if 'T+1' in message:
        return dict(title='今天刚买入的股票，今天不能卖。',next='普通A股要到下一个交易日才可卖出；也请检查记录的股数。')
    if '真实资金' in message:
        return dict(title='还没有填写你准备投入的钱。',next='先填写金额，再记录真实买卖。')
    if '资金不足' in message:
        return dict(title='这笔买入超过了账本里的可用资金。',next='减少股数，或核对投入金额和已有成交记录。')
    if '数据' in message or 'BaoStock' in message or '连接' in message or '交易日历' in message:
        return dict(title='这次没有取得完整行情，已保存的数据还在。',next='检查网络后点“更新行情”；更新完成前先不要按旧计划买入。')
    return dict(title='这一步没有完成。',next='可以重试；具体原因可在下面展开查看。')

def daily_decision(settings,stocks,rec,expected,job):
    available=[s for s in stocks if s.get('last')]
    base=dict(ready=False,buy_count=0,asof=max([s['last']['date'] for s in available],default=None),mode=settings['mode'],capital=settings['real_capital'] if settings.get('real_capital') else settings['sim_capital'],capital_kind='real' if settings.get('real_capital') else 'sim')
    def result(status,title,reason,action): return base|dict(status=status,title=title,reason=reason,next=action)
    if job.get('status')=='running' and job.get('kind')=='update':
        return result('updating','正在更新，先别按旧计划买入','软件正在检查最新收盘数据和买入条件。','更新完成后，这里会自动显示结果。')
    if not available:
        return result('no_data','先更新行情，再看今天的安排','还没有取得股票的真实价格。','点击“更新行情”，等进度完成。')
    if len(available)!=len(stocks) or len({s['last']['date'] for s in available})!=1:
        return result('incomplete','行情还没更新完整，今天先不按这些计划买入','有些股票缺数据，或各股票的价格日期不一致。','点击“更新行情”，补齐后再看。')
    if expected and base['asof']<expected:
        return result('stale','行情日期落后，先更新再决定','现在看到的是 '+base['asof']+' 的收盘价，不能当作最新价格。','点击“更新行情”；失败时保留原数据，但不把旧计划当新机会。')
    if not rec:
        return result('no_plan','行情已有，买卖计划还没生成','还没有当前模式的选股结果。','点击“重新整理计划”。')
    if set(rec.get('universe',[]))!={s['code'] for s in stocks} or rec.get('asof')!=base['asof'] or rec.get('capital',rec.get('sim_capital'))!=base['capital'] or rec.get('capital_kind','sim')!=base['capital_kind']:
        return result('changed','你的设置已变，旧计划先不用','投入金额、观察股票或行情与原计划不一致。','点击“重新整理计划”，重新计算股票和股数。')
    if rec.get('stale'):
        return result('stale','这个计划用的是旧行情，今天先不用','先取得最近完整的收盘数据，再生成新的计划。','点击“更新行情”。')
    if '已过' in rec.get('validity',''):
        return result('expired','今天先不按旧计划买入','这批计划对应的开盘时间已经过去。错过就等下一批，不按旧价格追买。','收盘后更新行情，查看下一个交易日的计划。')
    plans=rec.get('plans',[])
    if not plans:
        return result('no_opportunity','目前没有符合条件的股票，可以先不买','最近一次检查，没有股票同时满足当前模式的买入规则。','有持仓就看下面的卖出提醒；没有持仓就等下次更新。')
    if not rec.get('entry_date'):
        return result('calendar_pending','先确认下一个开盘日期，再看买入安排','股票已有筛选结果，但还没取得下一个交易日的日历。','点击“更新行情”，软件会连同交易日历一起补齐。')
    affordable=[p for p in plans if p.get('quantity',0)>=100]
    if not affordable:
        return result('unaffordable','有股票符合规则，但按当前资金分配暂时不买','按这个模式的投入比例和亏损限制，分给每只股票的钱不足买一手（100股）。','不必为了买入而提高风险；可以继续观察或重新填写资金。')
    if not settings.get('real_capital'):
        return result('needs_money','有入选股票，先填你的金额再看买多少','现在的股数是按示例金额计算的，还没有按你自己的钱计算。','点击“填写我准备投入的钱”，保存后会自动重新算股数。')
    entry=rec['entry_date']
    return result('conditional','为 '+str(int(entry[5:7]))+'月'+str(int(entry[8:10]))+'日准备：'+str(len(affordable))+' 只股票等条件满足','这只是有条件的计划。只有开盘价格在范围内、券商显示可交易时，才考虑买入。','当天开盘时查看券商价格；超出范围就跳过。')|dict(ready=True,buy_count=len(affordable),entry_date=entry)
