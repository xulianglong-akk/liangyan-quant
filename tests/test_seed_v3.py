"""Quality seed hypotheses: causal indicators, risk caps, and measured similarity."""
import copy
import math
import statistics
import sys
import unittest
from datetime import date,timedelta
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from rules_v3 import PARAMS,LIMITS,COSTS,signal,make_plan,market_context,select_diverse,quantity,holding_period
from research_v3 import compiled_signals,replay

def fixture(n=180,growth=.0018,phase=0,code='sh.600000'):
    data=[]; day=date(2024,1,2); close=10.
    while len(data)<n:
        if day.weekday()<5:
            i=len(data); previous=close; close*=1+growth+.001*math.sin(i/4+phase)
            data.append(dict(date=day.isoformat(),code=code,open=previous,high=max(previous,close)*1.01,low=min(previous,close)*.99,close=close,preclose=previous,volume=20_000_000.,amount=200_000_000.,tradestatus='1',isST='0'))
        day+=timedelta(days=1)
    return data

def correlation_fixture(values,code):
    data=fixture(len(values)+1,growth=0,code=code); close=10
    for i,b in enumerate(data):
        previous=close
        if i: close*=1+values[i-1]
        b.update(open=previous,close=close,preclose=previous,high=max(previous,close)*1.01,low=min(previous,close)*.99)
    return data

class SeedRulesTests(unittest.TestCase):
    def setUp(self):
        self.stock=fixture(); self.benchmark=fixture(growth=.0005,phase=1.2,code='sh.000300'); self.params=PARAMS['steady'].copy()
    def test_quality_seed_has_two_holding_periods_and_fixed_limits(self):
        self.assertEqual((PARAMS['steady']['hold_short'],PARAMS['steady']['hold']),(15,25))
        self.assertEqual((PARAMS['active']['hold_short'],PARAMS['active']['hold']),(8,15))
        s=dict(family='breakout',volatility=.005)
        self.assertEqual(holding_period(s,self.params),15)
        self.assertEqual(holding_period(s|dict(family='trend'),self.params),25)
        with self.assertRaises(ValueError): quantity(10,9,22000,22000,0,'steady',exposure_scale=1.1)
    def test_missing_or_stale_benchmark_fails_closed_and_legacy_remains_usable(self):
        self.assertIsNone(signal(self.stock,self.params))
        self.assertIsNone(signal(self.stock,self.params,self.benchmark[:-1]))
        self.assertIsNone(make_plan('sh.600000','测试',self.stock,self.params,'steady',22000,22000,0,'seed'))
        self.assertIsNotNone(signal(self.stock,self.params|dict(quality=False)))
        for mode in PARAMS: self.assertFalse(market_context(None,PARAMS[mode],mode)['allow_new'])
    def test_current_and_sustained_liquidity_are_both_required(self):
        self.assertIsNotNone(signal(self.stock,self.params,self.benchmark))
        poor=copy.deepcopy(self.stock); poor[-1]['amount']=self.params['min_amount']-1
        self.assertIsNone(signal(poor,self.params,self.benchmark))
        thin=copy.deepcopy(self.stock)
        for b in thin[-20:-1]: b['amount']=1_000_000
        self.assertIsNone(signal(thin,self.params,self.benchmark))
    def test_extension_filter_does_not_chase_a_jump(self):
        jump=copy.deepcopy(self.stock); jump[-1]['close']*=1.10; jump[-1]['high']=jump[-1]['close']*1.01
        permissive=self.params|dict(max_vol=.1,max_extension=100.)
        self.assertIsNotNone(signal(jump,permissive,self.benchmark))
        self.assertIsNone(signal(jump,permissive|dict(max_extension=2.),self.benchmark))
    def test_relative_strength_changes_score_without_seeing_the_future(self):
        stronger=fixture(growth=.0014,phase=1.2,code='sh.000300')
        a=signal(self.stock,self.params,self.benchmark); b=signal(self.stock,self.params,stronger)
        self.assertIsNotNone(a);self.assertIsNotNone(b);self.assertGreater(a['score'],b['score'])
        future=copy.deepcopy(self.benchmark); future+=fixture(10,growth=.2,code='sh.000300')
        # Give future records genuinely later dates rather than duplicate past dates.
        for i,row in enumerate(future[-10:]): row['date']=(date.fromisoformat(self.stock[-1]['date'])+timedelta(days=i+1)).isoformat()
        self.assertEqual(signal(self.stock,self.params,future),a)
    def test_weak_market_blocks_steady_and_reduces_active_budget(self):
        weak=fixture(growth=-.002,phase=1.2,code='sh.000300')
        self.assertFalse(market_context(weak,self.params,'steady')['allow_new'])
        context=market_context(weak,PARAMS['active'],'active')
        self.assertTrue(context['allow_new']);self.assertEqual(context['exposure_scale'],.5)
        self.assertEqual(quantity(10,9,22000,22000,9000,'active',exposure_scale=context['exposure_scale']),0)
    def test_vector_and_daily_signal_agree_on_nonempty_prefixes(self):
        for mode,p in PARAMS.items():
            compiled=compiled_signals(self.stock,p,self.benchmark)
            self.assertGreater(len(compiled),0)
            for i in range(61,len(self.stock)+1,7):
                plain=signal(self.stock[:i],p,self.benchmark); vector=compiled.get(self.stock[i-1]['date'])
                self.assertEqual(bool(plain),bool(vector))
                if plain:
                    for key in ('atr','score','momentum','momentum60','relative20','relative60','extension_atr','volatility','fast','slow'):
                        self.assertAlmostEqual(plain[key],vector[key],places=7,msg=key)
                    self.assertEqual(plain['family'],vector['family'])
            self.assertEqual(compiled_signals(self.stock[:110],p,self.benchmark[:110]),{d:v for d,v in compiled.items() if d<=self.stock[109]['date']})
    def test_later_bad_benchmark_does_not_erase_earlier_signals(self):
        original=compiled_signals(self.stock,self.params,self.benchmark)
        bad=copy.deepcopy(self.benchmark); bad[-1]['preclose']=0
        changed=compiled_signals(self.stock,self.params,bad)
        self.assertEqual({d:v for d,v in original.items() if d<bad[-1]['date']},changed)
    def test_return_correlation_filters_candidates_and_existing_positions(self):
        x=[math.sin(i*.5) for i in range(60)]; x=[v-statistics.mean(x) for v in x]
        z=[math.cos(i*.2) for i in range(60)]; z=[v-statistics.mean(z) for v in z]
        projection=sum(a*b for a,b in zip(x,z))/sum(v*v for v in x); z=[b-projection*a for a,b in zip(x,z)]
        scale=math.sqrt(sum(v*v for v in x)/sum(v*v for v in z)); z=[v*scale for v in z]
        y=[.85*a+math.sqrt(1-.85**2)*b for a,b in zip(x,z)]
        codes=['sh.600000','sh.600001']; history={c:correlation_fixture([.001+.003*v for v in values],c) for c,values in zip(codes,[x,y])}
        plans=[dict(code=c,name=c,score=2-i,signal=dict(asof=history[c][-1]['date'])) for i,c in enumerate(codes)]
        items,rejected=select_diverse(plans,history,'steady');self.assertEqual(len(items),1);self.assertAlmostEqual(rejected[0]['correlation'],.85,places=5)
        self.assertEqual(len(select_diverse(plans,history,'active')[0]),2)
        items,rejected=select_diverse(plans[1:],history,'steady',held_codes=[codes[0]]);self.assertFalse(items);self.assertEqual(rejected[0]['with_code'],codes[0])
        later=copy.deepcopy(history)
        for c in codes:
            extra=copy.deepcopy(later[c][-1]);extra.update(date='2099-01-01',close=1000);later[c].append(extra)
        self.assertEqual(select_diverse(plans,later,'steady'),select_diverse(plans,history,'steady'))
    def test_replay_context_and_signal_are_causal(self):
        metadata={'sh.600000':dict(ipo='2000-01-01',delisted='')}; start=self.stock[61]['date']; end=self.stock[109]['date']
        full=replay({'sh.600000':self.stock},metadata,{},self.params,'steady',start,end,benchmark=self.benchmark)
        prefix=replay({'sh.600000':self.stock[:110]},metadata,{},self.params,'steady',start,end,benchmark=self.benchmark[:110])
        self.assertEqual(full,prefix);self.assertTrue(full['fills'])
        first=full['fills'][0];asof=next(i for i,b in enumerate(self.stock) if b['date']==first['date'])-1
        plan=make_plan('sh.600000','测试',self.stock[:asof+1],self.params,'steady',22000,22000,0,'seed',benchmark=self.benchmark)
        self.assertIsNotNone(plan);self.assertLessEqual(plan['buy_low'],first['price']);self.assertLessEqual(first['price'],plan['buy_high'])
        weak=fixture(growth=-.002,code='sh.000300')
        no_entry=replay({'sh.600000':self.stock},metadata,{},self.params,'steady',start,end,benchmark=weak)
        self.assertFalse(no_entry['fills'])
    def test_diversity_only_compares_the_group_that_can_be_selected(self):
        # Distinct phases produce different trailing changes; selection stops
        # at the account's three slots, rather than building a phantom portfolio.
        history={f'sh.60000{i}':fixture(80,phase=i*1.8,code=f'sh.60000{i}') for i in range(5)}
        plans=[dict(code=c,score=10-i,signal=dict(asof=bs[-1]['date'])) for i,(c,bs) in enumerate(history.items())]
        items,rejected=select_diverse(plans,history,'steady')
        self.assertEqual(len(items),3)
        self.assertTrue(any(r['reason']=='已达候选数量上限' for r in rejected))
        reduced,_=select_diverse(plans[1:],history,'steady',held_codes=[plans[0]['code']])
        self.assertLessEqual(len(reduced),2)
        full,rejected=select_diverse(plans,{},'steady',held_codes=['a','b','c'])
        self.assertFalse(full);self.assertTrue(all(r['reason']=='已达候选数量上限' for r in rejected))

if __name__=='__main__':unittest.main(verbosity=2)
