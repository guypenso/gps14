"""Read-only tax evidence and review queue. No provider calls or qualification writes."""
import datetime as dt
import json
from .collector import LIVE
from .core import tax_verification,number,verification
from .diagnostics import funnel
from .provenance import demo_marked
from .tax_backlog import priorities,review_deferred

UTC=dt.timezone.utc

def _time(value):
 try:
  parsed=dt.datetime.fromisoformat(str(value).replace('Z','+00:00'))
  return parsed.astimezone(UTC) if parsed.tzinfo else None
 except (TypeError,ValueError):return None

def provider_evidence(identity,row,now):
 """Expired provider evidence is historical, never a current verification."""
 if not row:return None
 raw,source,fetched,expires=row
 try:record=json.loads(raw)
 except (TypeError,ValueError):return None
 if source!=LIVE or not isinstance(record,dict) or record.get('id')!=identity or demo_marked(record):return None
 status,tax,reason=tax_verification(record,now.date())
 if not tax:return {'state':'unknown','source':'RentCast Property Records','fetched_at':fetched,'reason':reason}
 current=bool(_time(fetched) and _time(expires) and _time(fetched)<=now<_time(expires))
 return {'state':'provider_verified' if current and status in ('Qualified','Rejected') else 'historical',
         'annual':tax['annual'],'year':tax['year'],'fetched_at':fetched,'expires_at':expires,
         'source':'RentCast Property Records','qualification_gate':status if current else 'Pending Verification',
         'warning':None if current else 'Historical provider tax; current annual liability requires verification'}

def evidence(identity,history,estimate,now):
 provider=provider_evidence(identity,history,now)
 official=estimate if isinstance(estimate,dict) and estimate.get('state')=='estimated' and number(estimate.get('annual')) else None
 return {'state':provider['state'] if provider and provider['state']=='provider_verified' else 'official_data_estimate' if official else 'historical' if provider and provider['state']=='historical' else 'unknown',
         'provider':provider,'official_estimate':official,'parcel_id':estimate.get('parcel_id') if isinstance(estimate,dict) else None,
         'parcel_match':estimate.get('match_status','unmatched') if isinstance(estimate,dict) else 'unmatched',
         'official_bill':None,'gate_note':'Only current, matching annual tax evidence can pass the existing $100-buffer qualification rule.'}

def snapshot(store,boundary,now=None,offset=0,limit=25):
 now=now or dt.datetime.now(UTC)
 if not isinstance(offset,int) or not isinstance(limit,int) or offset<0 or offset>10000 or not 1<=limit<=25:raise ValueError('Bounded tax queue required')
 ranked=priorities(store,boundary,now)
 public=store.execute('SELECT payload FROM settings WHERE name=?',('public_tax_batch:2026',)).fetchone()
 batch=json.loads(public[0]) if public else {};estimates=batch.get('estimates',{}) if isinstance(batch.get('estimates',{}),dict) else {}
 histories={id:(raw,source,fetched,expires) for id,raw,source,fetched,expires in store.execute('SELECT property_id,payload,source,fetched_at,expires_at FROM tax_history').fetchall()}
 queue=[];summary={'estimated':0,'historical':0,'unknown':0,'unique_parcel':0,'ambiguous_parcel':0}
 for item in ranked:
  result=evidence(item['source_id'],histories.get(item['source_id']),estimates.get(item['source_id']),now)
  summary['estimated' if result['official_estimate'] else 'unknown']+=1
  if result['provider'] and result['provider']['state']=='historical':summary['historical']+=1
  if result['parcel_match']=='unique_parcel':summary['unique_parcel']+=1
  if result['parcel_match']=='ambiguous':summary['ambiguous_parcel']+=1
  queue.append({k:v for k,v in item.items() if k!='key'}|{'tax_evidence':result})
 source_manifest=batch.get('manifest') or {}
 coverage=funnel(store,boundary,now)
 # The funnel's historical owner_review_deferred counter only includes uncached
 # records. Decisions remain authoritative even when a tax cache exists.
 owner_deferred=sum(verification(json.loads(raw),boundary)[0]=='Candidate' and review_deferred(identity,store)
                    for identity,raw,source in store.execute('SELECT id,payload,source FROM properties').fetchall() if source==LIVE)
 return {'as_of':now.isoformat(),'source_records':coverage['counts'].get('stored_unique',0),
         'non_tax_pass':coverage['counts'].get('non_tax_pass',0),
         'non_tax_rejected':coverage['counts'].get('non_tax_rejected',0),
         'owner_deferred':owner_deferred,
         'broad_uncached':coverage['counts'].get('tax_uncached_backlog',0),
         'provider_cache_valid':coverage['counts'].get('valid_tax_cache',0),
         'provider_tax_qualified':coverage['counts'].get('tax_qualified',0),
         'provider_tax_rejected':coverage['counts'].get('tax_rejected',0),
         'actionable_total':len(ranked),'actionable_fresh':sum(r['fresh'] for r in ranked),
         'actionable_stale':sum(not r['fresh'] for r in ranked),
         'actionable_evidence':summary,'official_dataset':{k:source_manifest.get(k) for k in ('source','sha256','retrieved_at','year')},
         'source_access':{'wprdc':'Saved official assessment batch; current remote availability is not inferred from cached data.'},
         'queue':queue[offset:offset+limit],'offset':offset,'limit':limit,'next_offset':offset+limit if offset+limit<len(queue) else None,
         'qualification_changed':False,'provider_calls':0}
