"""Re-evaluate saved evidence without provider calls or history deletion."""
from .geography import EXCLUDED_NEIGHBORHOODS
import json
from .core import verification, tax_verification, EXCLUDED_ZIPS
from .collector import LIVE
from .provenance import demo_marked
from .digest import recent_timestamp


def reevaluate(store,boundary,now,*,atomic=True):
    from contextlib import nullcontext
    changed=0
    with store.transaction() if atomic else nullcontext():
        for identity,payload,old_state,old_reasons,source,observed in store.execute('SELECT id,payload,state,reasons,source,observed_at FROM properties').fetchall():
            row=json.loads(payload);state,reasons=verification(row,boundary)
            if state=='Candidate':
                cached=store.get_tax(identity,now.isoformat())
                if source!=LIVE:state,reasons='Pending Verification',['non_live_source']
                elif not recent_timestamp(observed,now):state,reasons='Pending Verification',['stale_listing']
                elif not cached:state,reasons='Pending Verification',['tax_cache_expired']
                elif cached[1]!=LIVE or not isinstance(cached[0],dict) or cached[0].get('id')!=identity or demo_marked(cached[0]):state,reasons='Pending Verification',['tax_identity_or_source_mismatch']
                else:
                    state,_,reason=tax_verification(cached[0],now.date());reasons=[reason] if reason else []
            if state!=old_state or reasons!=json.loads(old_reasons):
                store.set_verification(identity,state,reasons);changed+=1
        store.save_setting('criteria',dict(city='Pittsburgh',additional_municipality='Bellevue Borough',zip_15212_neighborhoods=['Brighton Heights','Spring Hill-City View'],excluded_neighborhoods=EXCLUDED_NEIGHBORHOODS,excluded_property_types=['Townhouse','Townhome','Rowhouse','Condominium'],state='PA',bedrooms_min=3,bathrooms_min=1,sqft_min=1000,price_max=200001,excluded_zip=sorted(EXCLUDED_ZIPS),tax_add=100,tax_max=2400,tax_comparison="<=",policy_version="investment-20261107-tax2400-inclusive",single_family_only=True))
    return changed


def reconcile_geography(store,boundary,now):
    """Stored-only, idempotent policy audit. Human review settings are untouched."""
    from .geography import POLICY
    key='geography_reconciliation:'+POLICY
    prior=store.execute('SELECT payload FROM settings WHERE name=?',(key,)).fetchone()
    if prior:return json.loads(prior[0])
    affected={};newly_excluded=0;previously_rejected=0;managed=0
    for identity,payload,state in store.execute('SELECT id,payload,state FROM properties').fetchall():
        p=json.loads(payload);geo,reason=boundary.evaluate(p)
        if geo!='Rejected' or reason not in {s.replace('-','_')+'_neighborhood' for s in boundary.pink_regions}:continue
        affected[reason]=affected.get(reason,0)+1
        if state=='Rejected':previously_rejected+=1
        else:newly_excluded+=1
        review=store.execute('SELECT payload FROM settings WHERE name=?',('penten_review:'+identity,)).fetchone()
        if review:
            d=json.loads(review[0])
            managed+=d.get('status') in ('pursue','monitor','missed_under_contract') or bool(d.get('contract_state'))
    with store.transaction():
        changed=reevaluate(store,boundary,now,atomic=False)
        result=dict(policy=POLICY,at=now.isoformat(),affected=sum(affected.values()),by_reason=affected,newly_excluded=newly_excluded,previously_rejected=previously_rejected,managed_preserved=managed,records_updated=changed,provider_calls=0)
        store.save_setting(key,result)
    return result
