"""Unitless, strategy-specific cohort ranks; no profitability rejection threshold."""
import math


def validate_weights(weights):
    if set(weights)!={'annual_return','monthly_cash_flow'} or any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or v<0 for v in weights.values()) or not math.isclose(sum(weights.values()),1,abs_tol=1e-9):
        raise ValueError('Two nonnegative ranking weights summing to1 are required')


def percentile(value,cohort):
    if not cohort:return None
    if len(cohort)==1:return 50.0
    below=sum(v<value for v in cohort);ties=sum(v==value for v in cohort)
    return 100*(below+(ties-1)/2)/(len(cohort)-1)


def rank(properties,*,weights=None):
    weights=weights if weights is not None else {'annual_return':.5,'monthly_cash_flow':.5};validate_weights(weights)
    profiles=sorted({name for p in properties for name in p.get('acquisition',{}).get('financed',{})})
    result={'cash':[],'financed':{name:[] for name in profiles},'weights':weights,'method':'Tied empirical percentiles:100*(below+(ties-1)/2)/(n-1); singleton/all ties50. Cohort-relative, not a probability or absolute return target.'}
    def cohort(strategy,profile=None):
        rows=[]
        for p in properties:
            if p['status']=='Rejected' or p.get('ranking_blockers',p.get('conflicts',{})) or p.get('original_screening')!='Qualified':continue
            data=p.get('acquisition',{});scenario=data.get('cash') if strategy=='cash' else data.get('financed',{}).get(profile)
            if not scenario or not scenario['complete']:continue
            metric='net_yield_pct' if strategy=='cash' else 'cash_on_cash_pct';m=scenario['metrics'];annual=m.get(metric);monthly=m.get('cash_flow_monthly')
            if annual is None or monthly is None or not math.isfinite(annual) or not math.isfinite(monthly):continue
            rows.append((p,scenario,annual,monthly))
        for p,scenario,annual,monthly in rows:
            rp=percentile(annual,[r[2] for r in rows]);cp=percentile(monthly,[r[3] for r in rows]);score=weights['annual_return']*rp+weights['monthly_cash_flow']*cp
            entry=dict(new=p.get('new',False),price_reduction=p.get('price_reduction',False),seen_this_window=p.get('seen_this_window',False),id=p['id'],address=p['address'],score=round(score,4),annual_return=annual,annual_return_metric='net_yield_pct' if strategy=='cash' else 'cash_on_cash_pct',monthly_cash_flow=monthly,
                return_percentile=round(rp,4),cash_flow_percentile=round(cp,4),cohort_size=len(rows),provisional=p['status']!='Qualified' or scenario['provisional'] or p.get('synthetic',False),
                initial_cash=scenario['metrics']['total_initial_cash'],reasons=f"{weights['annual_return']:.0%} return percentile + {weights['monthly_cash_flow']:.0%} monthly-flow percentile; same strategy/profile cohort only")
            yield entry
    result['cash']=sorted(cohort('cash'),key=lambda r:(-r['score'],r['id']))
    for profile in profiles:result['financed'][profile]=sorted(cohort('financed',profile),key=lambda r:(-r['score'],r['id']))
    return result
