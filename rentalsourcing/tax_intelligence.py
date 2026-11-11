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
 from .config import Settings
 settings=Settings.from_env()
 budget=store.budget_health(settings,now,record_warning=False)
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
 quality_row=store.execute('SELECT payload FROM settings WHERE name=?',('property_quality:last',)).fetchone()
 quality=json.loads(quality_row[0]) if quality_row else None
 from .property_types import property_type_evidence,reviewed_type
 recent=[]
 decisions={name[len('penten_review:'):]:json.loads(raw) for name,raw in store.execute('SELECT name,payload FROM settings WHERE name LIKE ?',('penten_review:%',)).fetchall()}
 for identity,raw,observed,source_state,source_reasons in store.execute('SELECT id,payload,observed_at,state,reasons FROM properties ORDER BY observed_at DESC LIMIT 160').fetchall():
  p=json.loads(raw)
  if p.get('bedrooms')==2 and p.get('price',999999)<=150000 and (verification(p,boundary)[0]!='Rejected' or reviewed_type(p)):
   recent.append({'source_id':identity,'address':p.get('formattedAddress'),'observed_at':observed,'property_type':property_type_evidence(p),'source_state':source_state,'source_reasons':json.loads(source_reasons),'penten_decision':decisions.get(identity)})
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
         'api_budget':{'dashboard_runtime':dict(budget,approval_cap=settings.approved_requests,rentcast_enabled=settings.enabled,email_enabled=settings.email_enabled),'last_scheduled':json.loads(store.execute('SELECT payload FROM settings WHERE name=?',('health_budget',)).fetchone()[0]) if store.execute('SELECT payload FROM settings WHERE name=?',('health_budget',)).fetchone() else None,'ledger':[{'period':r[0],'used':r[1]} for r in store.execute('SELECT period,used FROM usage ORDER BY period').fetchall()],'scope_note':'Dashboard provider calls are disabled. Scheduled cron configuration is separate; ledger and last_scheduled describe stored discovery usage.'},'property_quality':quality,'recent_source_type_evidence':recent,'owner_exception_source':__import__('rentalsourcing.owner_exception',fromlist=['snapshot']).snapshot(store,now),'qualification_changed':False,'provider_calls':0}
