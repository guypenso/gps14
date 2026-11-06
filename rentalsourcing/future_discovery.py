"""First-discovery accounting and email-independent inventory delivery.

Uses existing validated provider cache and saved assessment evidence only. No
government billing source is claimed connected without successful acquisition.
"""
import json
from .review_inventory import run,select
from .sync_runtime import component,incident


def process(store,boundary,job_id,now,*,config=None,opener=None):
    first=[json.loads(raw) for raw, in store.execute('SELECT payload FROM settings WHERE name LIKE ?',('first_discovery:%',)).fetchall()]
    records=[r for r in first if r['job_id']==job_id]
    identities={r['source_id'] for r in records}
    cards,counts=select(store,boundary,now,source_ids=identities,include_verified=True)
    batch=store.execute('SELECT payload FROM settings WHERE name=?',('public_tax_batch:2026',)).fetchone()
    estimates=json.loads(batch[0]).get('estimates',{}) if batch else {}
    verified=sum(c['tax_status']=='provider_verified' for c,p in cards)
    estimated=sum(c['tax_status']!='provider_verified' and estimates.get(c['source_id'],{}).get('state')=='estimated' for c,p in cards)
    report={'job_id':job_id,'new_source_listings':len(records),'new_non_tax_pass':sum(r['non_tax_state']=='Candidate' for r in records),
            'new_parcel_matches':sum(estimates.get(i,{}).get('match_status')=='unique_parcel' for i in identities),
            'new_verified_tax':verified,'new_estimated_tax':estimated,'new_unknown_tax':len(cards)-verified-estimated,
            'new_qualified':verified,'new_pending':len(cards)-verified,'new_rejected':counts['hard_rejected']+counts['tax_rejected'],
            'synced':0,'paid_tax_calls':0,'state':'not_configured' if not config else 'pending',
            'source_limitations':['No validated automated official annual-bill source is connected. Assessment estimates do not verify annual liability.']}
    key='future_discovery_report:'+job_id
    store.save_setting(key,report)
    if not records or not cards:
        report['state']='no_new_reviewable_records';store.save_setting(key,report);return report
    if config:
        try:
            ledger=run(store,boundary,now,execute=True,config=config,opener=opener,
                       job_key='stored-review-inventory:future:'+job_id,source_ids=identities,include_verified=True,refresh_decisions=False)
            report.update(state='accepted',synced=sum(len(json.loads(b['payload'])['opportunities']) for b in ledger['batches']))
            component(store,'inventory_sync','success',now=now,processed=report['synced'])
            incident(store,'inventory_sync',None,now=now,resolved=True)
        except Exception as exc:
            report.update(state='pending_recovery',error_type=type(exc).__name__)
            error={'category':'future_inventory_delivery','retryable':True}
            component(store,'inventory_sync','partial',now=now,pending=len(cards),error=error)
            incident(store,'inventory_sync',error,now=now)
    store.save_setting(key,report)
    return report
