"""Re-evaluate saved evidence without provider calls or history deletion."""
from .geography import EXCLUDED_NEIGHBORHOODS
import json
from .core import verification, tax_verification, EXCLUDED_ZIPS
from .collector import LIVE
from .provenance import demo_marked
from .digest import recent_timestamp


def reevaluate(store,boundary,now,*,atomic=True):
    from .tax_backlog import review_deferred
    from contextlib import nullcontext
    changed=0
    with store.transaction() if atomic else nullcontext():
        for identity,payload,old_state,old_reasons,source,observed in store.execute('SELECT id,payload,state,reasons,source,observed_at FROM properties').fetchall():
            if review_deferred(identity,store):continue
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
        store.save_setting('criteria',dict(city='Pittsburgh',approved_municipalities=['Pittsburgh','Bellevue Borough','Pleasant Hills Borough','Bethel Park Municipality'],county='Allegheny',high_rent_exception_monthly_strictly_above=2300,zip_15212_neighborhoods=['Brighton Heights','Spring Hill-City View'],excluded_neighborhoods=EXCLUDED_NEIGHBORHOODS,excluded_property_types=['Townhouse','Townhome','Rowhouse','Condominium'],state='PA',bedrooms_min=2,maximum_price_by_bedrooms={2:150000,3:200001,4:200001},bathrooms_min=1,sqft_min=1000,price_max=200001,excluded_zip=sorted(EXCLUDED_ZIPS),tax_add=100,tax_max=2400,tax_comparison="<=",policy_version="investment-20261111-two-bedroom",single_family_only=True))
    return changed

def reconcile_bedroom_policy(store,boundary,now):
    """Only automated bedroom rejections; preserve history and owner decisions."""
    from .investment_policy import POLICY_VERSION
    from .tax_backlog import review_deferred
    from .sync_runtime import get
    key='bedroom_policy_reconciliation:'+POLICY_VERSION
    prior=get(store,key)
    if prior:return prior
    result={'policy':POLICY_VERSION,'at':now.isoformat(),'two_bedroom_records':0,'new_non_tax_eligible':0,'human_decisions_preserved':0,'stale_held':0,'fresh_candidates':0,'qualified':0,'pending_verification':0,'source_ids':[],'provider_calls':0}
    history=[]
    for identity,raw,state,reasons,source,observed in store.execute('SELECT id,payload,state,reasons,source,observed_at FROM properties').fetchall():
        p=json.loads(raw)
        if p.get('bedrooms')!=2:continue
        result['two_bedroom_records']+=1
        if review_deferred(identity,store):result['human_decisions_preserved']+=1;continue
        if verification(p,boundary)[0]!='Candidate':continue
        if state!='Rejected' or set(json.loads(reasons))!={'bedrooms'}:continue
        result['new_non_tax_eligible']+=1
        history.append({'source_id':identity,'previous_state':state,'previous_reasons':json.loads(reasons),'policy':POLICY_VERSION})
        if source!=LIVE or not recent_timestamp(observed,now):result['stale_held']+=1;continue
        tax=store.get_tax(identity,now.isoformat());status='Pending Verification';why=['tax_verification_required']
        if tax and tax[1]==LIVE and tax[0].get('id')==identity:
            status,_,reason=tax_verification(tax[0],now.date());why=[reason] if reason else []
        store.set_verification(identity,status,why)
        if status=='Rejected':continue
        result['source_ids'].append(identity);result['fresh_candidates']+=1
        result['qualified' if status=='Qualified' else 'pending_verification']+=1
    result['history']=history;store.save_setting(key,result)
    return result


def reconcile_geography(store,boundary,now):
    """Stored-only, idempotent policy audit. Human review settings are untouched."""
    from .geography import POLICY
    key='geography_reconciliation:'+POLICY
    prior=store.execute('SELECT payload FROM settings WHERE name=?',(key,)).fetchone()
    if prior:return json.loads(prior[0])
    affected={};newly_excluded=0;previously_rejected=0;managed=0;expanded=[];municipal_counts={name:0 for name in boundary.additional_municipalities}
    from .tax_backlog import review_deferred
    for identity,payload,state in store.execute('SELECT id,payload,state FROM properties').fetchall():
        p=json.loads(payload);geo,reason=boundary.evaluate(p)
        municipality=boundary.municipality(p)
        if municipality in municipal_counts:
            municipal_counts[municipality]+=1
            record=store.get_property(identity)
            if verification(p,boundary)[0]=='Candidate' and recent_timestamp(record['observed_at'],now) and not review_deferred(identity,store):expanded.append(identity)
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
        result=dict(policy=POLICY,at=now.isoformat(),affected=sum(affected.values()),by_reason=affected,newly_excluded=newly_excluded,previously_rejected=previously_rejected,managed_preserved=managed,records_updated=changed,provider_calls=0,municipal_stored=municipal_counts,new_reviewable_ids=expanded,expanded_reviewable=len(expanded))
        store.save_setting(key,result)
    return result
