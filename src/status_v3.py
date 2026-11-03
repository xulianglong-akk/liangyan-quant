"""Small, read-only explanations of work and candidate verification state.

These summaries describe recorded facts. They never start jobs, change accounts,
or infer candidate observation from the ordinary paper account.
"""
import json
from datetime import datetime

from rules_v3 import PARAMS, TZ, fresh_quote, trading_session


def _text(value):
    return str(value or '').strip()


def _account(store, key):
    try:
        return store.account(key)
    except ValueError:
        return None


def _distinct_days(account):
    return set((account or {}).get('days', []))


def activity_summary(store, mode, session=None, *, account_id=None):
    """Describe the selected account's latest plan without a history payload."""
    if mode not in PARAMS:
        raise ValueError('模式无效')
    clock = datetime.now(TZ)
    today = clock.date().isoformat()
    settings = store.get('settings', {})
    account_id = account_id or ('real' if settings.get('stage') == 'real' else 'paper-' + mode)
    account = _account(store, account_id) or {}
    work = store.get('work', {})
    coverage = store.coverage()
    plan = store.latest_plan(account_id, mode=mode) or {}
    health = store.get('quote_health', {})
    quotes = store.get_quotes()
    session = session or trading_session(clock, store.calendar())
    items = plan.get('items', [])
    current_plan = plan.get('day') == today
    needed = set(account.get('positions', {}))
    if current_plan:
        needed.update(p['code'] for p in items)
    source_times = [_text(quotes[c].get('source_time')) for c in needed if c in quotes]
    result = dict(
        status='waiting', label='等待分析', reason='',
        last_analysis_at=plan.get('created'), screened=plan.get('screened', 0),
        last_result_reason='尚未保存选股结果',
        candidate_count=len(items), plan_day=plan.get('day'),
        quote_checked_at=health.get('fetched'),
        quote_source_at=max(source_times, default=None), details=[],
    )
    details = result['details']
    if plan.get('day'):
        details.append('计划适用日期：' + plan['day'])
    if plan.get('asof'):
        details.append('选股使用的已完成行情：' + plan['asof'])
    market_reason = _text(plan.get('market', {}).get('reason'))
    if plan:
        if plan.get('market', {}).get('allow_new') is False and market_reason:
            result['last_result_reason'] = market_reason
        elif not items:
            result['last_result_reason'] = '上次选股没有股票同时满足本模式的买入条件'
        else:
            result['last_result_reason'] = f'上次选股形成{len(items)}个候选，达到买入条件才成交'
    if market_reason:
        details.append(market_reason)
    excluded = plan.get('excluded', {})
    for reason, count in sorted(excluded.items(), key=lambda item: (-item[1], item[0]))[:6]:
        details.append(f'{reason}：{count}只')

    def finish(status, label, reason):
        result.update(status=status, label=label, reason=reason)
        return result

    if work.get('status') == 'error':
        error = _text(work.get('error') or work.get('message'))
        return finish('error', '运行异常，已暂停', error or '请查看数据与设置中的运行记录')
    if not work.get('active'):
        return finish('paused', '已暂停', '点击“开始工作”继续分析和模拟')
    if not coverage.get('complete'):
        reason = f"历史数据准备中：{coverage.get('prepared', 0)}/{coverage.get('eligible', 0)}只"
        if coverage.get('errors'):
            details.insert(0, f"有{coverage['errors']}只数据未完成，可在“数据与设置”重试")
        return finish('preparing', '数据准备中', reason)
    if session == 'unknown':
        return finish('preparing', '数据准备中', '交易日历尚未准备完整')
    if plan and plan.get('asof') != coverage.get('asof'):
        return finish('analyzing', '等待新计划', '历史行情已更新，上次计划不能用于当前选股')
    if session == 'trading' and needed:
        # The monitor uses these same source timestamps. The fetch time alone is
        # never evidence that a quote is current, and other accounts' missing
        # quotes do not make this account unhealthy.
        stale = [c for c in sorted(needed) if c not in quotes or not fresh_quote(quotes[c], clock)]
        if health.get('error') or stale:
            if health.get('error'):
                details.insert(0, _text(health['error']))
            if stale:
                details.insert(0, f'本模式有{len(stale)}只行情缺失或超过120秒')
            return finish('quote_paused', '价格触发暂停', '行情连接失败、过期或缺失，暂不模拟成交')
    if not plan:
        return finish('analyzing', '等待完成选股', '全市场历史已准备，尚未形成买入计划')
    if plan.get('day', '') < today:
        return finish('analyzing', '等待新计划', '上次计划已到期，等待下一次全市场选股')
    if account.get('halt'):
        return finish('risk_paused', '暂停新增买入', '已达到账户亏损限制；已有持仓仍按退出条件观察')
    if not items:
        reason = market_reason if plan.get('market', {}).get('allow_new') is False else '没有股票同时满足本模式的买入条件'
        if account.get('positions'):
            reason += '；已有持仓继续观察'
        if plan.get('day') != today:
            reason += '（下一交易日计划）'
        return finish('no_opportunity', '暂无符合条件的股票', reason)
    if plan.get('day') != today:
        return finish('waiting', '下一交易日计划已准备', f"{len(items)}个候选，计划适用日期为{plan.get('day')}")
    if session != 'trading':
        reason = dict(lunch='午间休市，下午开市后继续观察', auction='收盘集合竞价，暂停模拟成交',
                      closed='当前闭市，开市后按有效计划继续观察', holiday='今天不是交易日，等待下一个交易日').get(session, '等待交易时段')
        return finish('waiting', '等待交易时段', reason)
    pending = [p for p in items if p['code'] not in account.get('positions', {})]
    return finish('monitoring', '正在观察价格',
                  f'{len(pending)}个候选等待满足买入条件；符合条件才模拟成交' if pending
                  else '候选已持有，正在观察退出条件')


def evolution_progress(store):
    """Progress is tied to the frozen candidate and its shadow/control pair."""
    work = store.get('work', {})
    coverage = store.coverage()
    wait = _text(store.get('research_wait'))
    clock = datetime.now(TZ)
    session = trading_session(clock, store.calendar())
    output = {}
    with store.db() as db:
        rows = [dict(r) | dict(payload=json.loads(r['payload'])) for r in db.execute('SELECT * FROM strategies')]
        experiments = {mode: db.execute('SELECT created,payload FROM experiments WHERE mode=? ORDER BY rowid DESC LIMIT 1', (mode,)).fetchone() for mode in PARAMS}
    for mode in PARAMS:
        candidate = next((r for r in rows if r['mode'] == mode and r['status'] == 'challenger'), None)
        current = next((r for r in rows if r['mode'] == mode and r['status'] == 'current'), None)
        experiment = experiments[mode]
        experiment_value = json.loads(experiment['payload']) if experiment else {}
        progress = store.get('research-progress-' + mode, {})
        phase = _text((progress or {}).get('phase'))
        result = dict(stage='waiting', label='后续观察未开始', reason='', candidate_id=None,
                      days=0, required_days=20, trades=0, required_trades=10,
                      remaining_days=20, remaining_trades=10,
                      last_check_at=experiment['created'] if experiment else None, details=[],
                      progress=progress or {}, wait=wait, completed=bool(experiment),
                      can_run=bool(coverage.get('complete') and work.get('active')),
                      running=bool(work.get('active') and phase in ('snapshot', 'training', 'validation', 'test')),
                      last_attempt_at=(progress or {}).get('updated') or (experiment['created'] if experiment else None))
        details = result['details']
        if candidate:
            cid = candidate['id']
            shadow = _account(store, 'shadow-' + cid)
            control = _account(store, 'control-' + cid)
            days = len(_distinct_days(shadow) & _distinct_days(control))
            trades = (shadow or {}).get('closed', 0)
            result.update(candidate_id=cid, days=days, trades=trades,
                          remaining_days=max(0, 20-days), remaining_trades=max(0, 10-trades),
                          last_check_at=candidate['payload'].get('created'), stage='forward', label='新策略正在独立模拟')
            common = _distinct_days(shadow) & _distinct_days(control)
            if common:
                details.append('最近共同观察交易日：' + max(common))
            details.append('观察日仅统计新策略与对照策略均获得有效行情的交易日')
            if (shadow or {}).get('warnings') or (control or {}).get('warnings'):
                details.append('候选和对照账户的现金分红按20%预留税，结果偏保守')
            if shadow is None or control is None:
                result.update(stage='blocked', label='候选观察暂缓', reason='候选或对照模拟账户缺失，尚不能验证升级')
            elif shadow.get('invalid') or control.get('invalid'):
                result.update(stage='blocked', label='候选验证受阻', reason='存在尚未准确处理的权益事件，不能用当前结果证明升级')
                details.extend((_text(x) for x in (shadow.get('invalid', []) + control.get('invalid', []))[:3]))
            elif shadow.get('halt'):
                result.update(stage='blocked', label='候选未达到要求', reason='新策略已达到暂停新增买入限制，等待后台确认失败并保留原版')
            elif shadow.get('receivables') or control.get('receivables'):
                result.update(stage='blocked', label='候选验证暂缓', reason='有待到账的分红或送股，升级检验暂缓')
            elif result['remaining_days'] or result['remaining_trades']:
                result['reason'] = f"还需{result['remaining_days']}个共同观察交易日、{result['remaining_trades']}笔完整模拟买卖"
            else:
                result['reason'] = '观察数量已达到起始门槛，仍需收益、亏损及费用条件通过'
            if not work.get('active'):
                details.insert(0, result['reason'])
                result.update(stage='paused' if work.get('status') != 'error' else 'error',
                              label='后续观察已暂停', reason=_text(work.get('error')) if work.get('status') == 'error' else '点击“开始工作”继续；已积累的观察记录保留')
        else:
            details.append('没有正在观察的冻结候选；当前模拟账户的天数和交易笔数不计入候选升级')
            if progress.get('message'):
                details.append('最近保存的历史比较进度：' + _text(progress['message']))
            if wait:
                details.insert(0, wait)
            if work.get('status') == 'error':
                result.update(stage='error', label='升级检验已暂停', reason=_text(work.get('error') or work.get('message')))
            elif not work.get('active'):
                result.update(stage='paused', reason='工作已暂停；点击“开始工作”后继续，后续观察尚未开始')
            elif not coverage.get('complete'):
                result.update(stage='preparing', reason='先补齐全市场历史数据，再检验新策略')
            elif not store.get('terminated_source_complete', False):
                result.update(stage='blocked', reason=wait if '退市名录' in wait else '退市名录尚未核实完整，暂停升级验证')
            elif result['running']:
                result.update(stage='historical', label='正在检验过去行情', reason=_text(progress.get('message')) or wait or '正在比较历史规则')
            elif wait and not wait.startswith(('比较策略中','本轮未通过','新候选已冻结','本轮检验')):
                result.update(stage='blocked', reason=wait)
            elif wait.startswith('比较策略中') and store.get('researched-' + mode) != clock.strftime('%G-W%V'):
                result.update(stage='historical', label='正在检验过去行情', reason=_text(progress.get('message')) or wait)
            elif experiment and not (experiment_value.get('gate') or {}).get('passed'):
                reasons = (experiment_value.get('gate') or {}).get('reasons', [])
                details.extend(_text(r) for r in reasons[:3])
                result.update(stage='history_failed', label='新策略未通过，继续原版', reason='最近候选未通过历史检验，后续观察尚未开始')
            elif current and (current['payload'].get('forward_evidence') or {}).get('passed'):
                result.update(stage='verified', label='当前版本已通过观察', reason='本次没有新的冻结候选，下一候选需重新独立验证')
            elif session in ('trading', 'lunch', 'auction'):
                result['reason'] = '价格观察优先，闭市后再比较新策略；后续观察尚未开始'
            else:
                result['reason'] = '等待后台比较新策略，先通过历史检验才开始后续观察'
        output[mode] = result
    return output
