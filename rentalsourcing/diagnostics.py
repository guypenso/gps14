"""Read-only aggregate evidence; never emits addresses, payloads or credentials."""
import json
from collections import Counter
from .core import verification,tax_verification
from .collector import LIVE
from .provenance import demo_marked


def funnel(store,boundary,now):
    rows=store.execute('SELECT id,payload,state,source,observed_at FROM properties').fetchall()
    counts=Counter();rejections=Counter();primary=Counter();missing=Counter()
    for identity,raw,saved_state,source,observed in rows:
        p=json.loads(raw);counts['stored_unique']+=1
        state,reasons=verification(p,boundary)
        if state=='Rejected':
            counts['non_tax_rejected']+=1;rejections.update(reasons);primary.update(reasons[:1]);continue
        if state!='Candidate':
            counts['non_tax_pending']+=1;missing.update(reasons);continue
        counts['non_tax_pass']+=1
        cached=store.get_tax(identity,now.isoformat())
        if not cached or cached[1]!=LIVE or not isinstance(cached[0],dict) or cached[0].get('id')!=identity or demo_marked(cached[0]):
            counts['tax_uncached_backlog']+=1;continue
        counts['valid_tax_cache']+=1
        tax_state,_,_=tax_verification(cached[0],now.date())
        counts[{'Qualified':'tax_qualified','Rejected':'tax_rejected','Pending Verification':'tax_pending_cached'}[tax_state]]+=1
    latest=None
    for raw,source in store.execute('SELECT payload,source FROM response_cache').fetchall():
        if source!=LIVE:continue
        body=json.loads(raw)
        if isinstance(body,dict) and isinstance(body.get('data'),list) and body.get('fetched_at') and (latest is None or body['fetched_at']>latest['fetched_at']):latest=body
    discovery=None
    if latest:
        data=latest['data'];ids=[p.get('id') for p in data if isinstance(p,dict) and p.get('id')]
        states=Counter(verification(p,boundary)[0] for p in data if isinstance(p,dict))
        discovery=dict(observed_at=latest['fetched_at'],received=len(data),unique_ids=len(set(ids)),duplicate_ids=len(ids)-len(set(ids)),provider_total=latest.get('total'),non_tax_pass=states['Candidate'],non_tax_pending=states['Pending Verification'],rejected=states['Rejected'])
    target=store.get_property('2106-Rockledge-St,-Pittsburgh,-PA-15212')
    targeted=None
    if target:
        p=target['payload'];geographic=boundary.evaluate(p)
        targeted=dict(state=target['state'],reasons=target['reasons'],listing_status=p.get('status'),observed_at=target['observed_at'],geography=geographic[0],property_type=p.get('propertyType'))
    return dict(targeted_recovery=targeted,counts=dict(counts),primary_rejections=dict(primary),rejection_reasons=dict(rejections),missing_reasons=dict(missing),latest_discovery=discovery)


def sync_health(store):
    rows=store.execute("SELECT r.synced_at,r.last_attempt_at,r.error_type,r.payload FROM command_center_reports r JOIN outbox o ON o.id=r.outbox_id WHERE o.state='sent'").fetchall()
    accepted=[r[0] for r in rows if r[0]];failed=[r[1] for r in rows if not r[0] and r[2] and r[1]]
    return dict(accepted_reports=len(accepted),pending_reports=sum(not r[0] for r in rows),last_success_at=max(accepted,default=None),last_unresolved_failure_at=max(failed,default=None),error_types=dict(Counter(r[2] for r in rows if not r[0] and r[2])),selected_cards=sum(len(json.loads(r[3]).get('opportunities',[])) for r in rows))
