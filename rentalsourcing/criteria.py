"""Re-evaluate saved evidence without provider calls or history deletion."""
import json
from .core import verification, tax_verification, EXCLUDED_ZIPS
from .collector import LIVE
from .provenance import demo_marked
from .digest import recent_timestamp


def reevaluate(store,boundary,now):
    changed=0
    with store.transaction():
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
        store.save_setting('criteria',dict(city='Pittsburgh',state='PA',bedrooms_min=3,bathrooms_min=1,sqft_min=1000,price_max=200000,excluded_zip=sorted(EXCLUDED_ZIPS),tax_add=100,tax_strict_max=2000))
    return changed
