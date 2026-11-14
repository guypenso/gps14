"""RentCast long-term AVM evidence. No inferred rent or automatic budget expansion."""
import datetime as dt,hashlib,json,os,urllib.parse
from .core import number

ENDPOINT='https://api.rentcast.io/v1/avm/rent/long-term'
def identity(p):
    from .offmarket import street_key
    return street_key(p)+'|'+str(p.get('zipCode') or p.get('zip_code') or '').split('-')[0]
def key(p):return 'rentcast-long-term:'+hashlib.sha256(identity(p).encode()).hexdigest()
def job_key(p):return 'rent-enrichment:'+hashlib.sha256(identity(p).encode()).hexdigest()

def record_job(store,p,state,now,reason=None,next_at=None):
    """One durable, identity-scoped state; never itself authorizes a paid call."""
    previous=store.execute('SELECT payload FROM settings WHERE name=?',(job_key(p),)).fetchone()
    old=json.loads(previous[0]) if previous else {}
    value={'source_id':p.get('id'),'subject_identity':identity(p),'state':state,'reason':reason,
           'updated_at':now.isoformat(),'next_at':next_at,'attempts':old.get('attempts',0)+(state=='Processing')}
    if old.get('state')!=state or old.get('reason')!=reason or old.get('next_at')!=next_at or old.get('source_id')!=p.get('id'):
        store.save_setting(job_key(p),value)
    return value

def normalize(raw,p,observed_at,now):
    try:
        when=dt.datetime.fromisoformat(observed_at.replace('Z','+00:00'))
        if not when.tzinfo or not dt.timedelta(0)<=now-when<=dt.timedelta(days=30):return None
        subject=raw.get('subjectProperty') or {}
        if identity(subject)!=identity(p) or subject.get('propertyType') not in (None,'Single Family'):return None
        for field in ('bedrooms','bathrooms','squareFootage'):
            if subject.get(field) is not None and subject[field]!=p.get(field):return None
        values=[raw.get(k) for k in ('rent','rentRangeLow','rentRangeHigh')]
        if not all(number(v) and 0<v<100000 for v in values) or not values[1]<=values[0]<=values[2]:return None
        comps=[]
        for c in raw.get('comparables',[])[:25]:
            if not isinstance(c,dict) or not c.get('formattedAddress') or not number(c.get('price')) or c['price']<=0:continue
            comps.append({k:c[k] for k in ('id','formattedAddress','propertyType','bedrooms','bathrooms','squareFootage','price','lastSeenDate','listedDate','distance','correlation') if k in c})
        if len(comps)<3:return None
        return {'source':'RentCast','method':'long_term_avm','source_url':ENDPOINT,'subject_identity':identity(p),'source_id':p.get('id'),
                'subject':{'address':p.get('formattedAddress'),'street_address':p.get('addressLine1'),'unit':p.get('addressLine2'),'zip_code':p.get('zipCode')},'monthly':values[0],'low':values[1],'high':values[2],'observed_at':when.isoformat(),'comparables':comps,
                'is_estimate':True,'high_rent_candidate':values[0]>2300}
    except (ValueError,TypeError,AttributeError):return None

def parameters(p):
    return {'address':p['formattedAddress'],'propertyType':'Single Family','bedrooms':p['bedrooms'],'bathrooms':p['bathrooms'],'squareFootage':p['squareFootage'],'compCount':10,'daysOld':90}

def cached_estimate(store,p,now):
    row=store.execute('SELECT payload FROM settings WHERE name=?',(key(p),)).fetchone()
    if not row:
        try:
            url=ENDPOINT+'?'+urllib.parse.urlencode(sorted(parameters(p).items()))
            existing=store.cache_get(hashlib.sha256(url.encode()).hexdigest(),now.isoformat())
            if existing and existing[1]=='rentcast-live':return normalize(existing[0].get('data',{}),p,existing[0].get('fetched_at',''),now)
        except (KeyError,ValueError,TypeError,AttributeError):pass
        return None
    saved=json.loads(row[0])
    return normalize(saved.get('raw',{}),p,saved.get('observed_at',''),now) if saved.get('source')=='RentCast' else None

def enrich(store,boundary,settings,now,client=None):
    from .core import verification
    from .tax_backlog import review_deferred
    from .collector import LIVE
    counts={'cached':0,'retrieved':0,'invalid':0,'paid_calls':0,'blocked_by_budget':0}
    candidates=[]
    records=[(i,json.loads(raw),observed) for i,raw,observed in store.execute('SELECT id,payload,observed_at FROM properties WHERE source=?',(LIVE,)).fetchall()]
    known={i for i,p,observed in records}
    for raw, in store.execute('SELECT payload FROM settings WHERE name LIKE ?',('listing_identity:%',)).fetchall():
        row=json.loads(raw);p=row.get('property',{});source_id=row.get('source_id')
        if not row.get('canonical_source_id') and source_id not in known and p.get('id')==source_id:
            records.append((source_id,p,row.get('observed_at','')));known.add(source_id)
    for source_id,p,observed in records:
        if review_deferred(source_id,store) or verification(p,boundary)[0]!='Candidate':continue
        if cached_estimate(store,p,now):counts['cached']+=1;record_job(store,p,'Completed',now);continue
        previous=store.execute('SELECT payload FROM settings WHERE name=?',(job_key(p),)).fetchone()
        queued=json.loads(previous[0]).get('state') if previous else None
        # A budget-blocked discovery remains eligible for a bounded future run
        # after approval; the daily 24-hour intake window must not erase it.
        if observed<(now-dt.timedelta(hours=24)).isoformat() and queued not in {'Budget Blocked','Pending','Retry Scheduled'}:continue
        if observed<(now-dt.timedelta(days=30)).isoformat():
            record_job(store,p,'Awaiting Evidence',now,'listing_observation_stale')
            continue
        first=store.execute('SELECT payload FROM settings WHERE name=?',('first_discovery:'+source_id,)).fetchone()
        high_tax=store.get_tax(source_id,now.isoformat())
        from .core import tax_verification
        potential_exception=high_tax and tax_verification(high_tax[0],now.date())[2]=='annual_tax_too_high'
        review=store.execute('SELECT payload FROM settings WHERE name=?',('penten_review:'+source_id,)).fetchone()
        status=json.loads(review[0]).get('status') if review else 'new'
        fresh=bool(first and json.loads(first[0]).get('at','')>=(now-dt.timedelta(days=1)).isoformat())
        priority=0 if status=='pursue' else 1 if status=='new' and fresh else 2 if potential_exception else 3 if status=='monitor' else 4
        candidates.append((priority,p))
    from .monthly_allocation import available
    approved=(os.getenv('ENABLE_RENT_ESTIMATES','false').lower()=='true'
              and settings.approval_mode=='monthly_budget' and not settings.supplemental_requests
              and available(store,settings,now,'rent')>0)
    permitted=approved and client is not None
    if not permitted:
        for _,p in candidates:record_job(store,p,'Pending' if approved else 'Budget Blocked',now,'awaiting_scheduled_rent_estimate' if approved else 'monthly_rent_budget_not_approved_or_inactive')
        counts['blocked_by_budget']=0 if approved else len(candidates)
        return counts
    for _,p in sorted(candidates,key=lambda x:(x[0],x[1]['id']))[:min(3,int(os.getenv('RENT_ESTIMATE_REQUESTS_PER_RUN','1')))]:
        if available(store,settings,now,'rent')<=0:
            for _,remaining in candidates:
                if not cached_estimate(store,remaining,now):record_job(store,remaining,'Budget Blocked',now,'rent_allocation_exhausted')
            counts['blocked_by_budget']=len(candidates)-counts['retrieved'];break
        old=store.execute('SELECT payload FROM settings WHERE name=?',('rent-attempt:'+key(p),)).fetchone()
        if old and json.loads(old[0]).get('next_at','')>now.isoformat():record_job(store,p,'Retry Scheduled',now,'provider_retry_cooldown',json.loads(old[0])['next_at']);continue
        store.save_setting('rent-attempt:'+key(p),{'next_at':(now+dt.timedelta(days=1)).isoformat()})
        record_job(store,p,'Processing',now)
        before=client.count
        try:
            data,_=client.get('/avm/rent/long-term',parameters(p),cache=True)
            value=normalize(data,p,client.last_fetched_at,now)
            if not value:counts['invalid']+=1;record_job(store,p,'Awaiting Evidence',now,'provider_response_failed_property_or_comparable_validation');continue
            store.save_setting(key(p),{'source':'RentCast','raw':data,'observed_at':client.last_fetched_at});counts['retrieved']+=1;record_job(store,p,'Completed',now)
            tax=store.get_tax(p['id'],now.isoformat())
            if tax and tax[1]==LIVE and tax[0].get('id')==p['id']:
                from .core import tax_verification
                state,_,reason=tax_verification(tax[0],now.date());store.set_verification(p['id'],state,[reason] if reason else [])
        except Exception as exc:
            retry=(now+dt.timedelta(days=1)).isoformat()
            store.save_setting('rent-attempt:'+key(p),{'next_at':retry,'error':type(exc).__name__})
            record_job(store,p,'Retry Scheduled',now,type(exc).__name__,retry)
        finally:counts['paid_calls']+=client.count-before
    return counts

def job_summary(store):
    rows=[json.loads(raw) for raw, in store.execute('SELECT payload FROM settings WHERE name LIKE ?',('rent-enrichment:%',)).fetchall()]
    states={state:sum(row.get('state')==state for row in rows) for state in ('Pending','Processing','Completed','Awaiting Evidence','Budget Blocked','Retry Scheduled','Failed')}
    recent=sorted(rows,key=lambda row:row.get('updated_at',''),reverse=True)[:20]
    return {'total':len(rows),'by_state':states,'recent':[{'source_id':row.get('source_id'),'state':row.get('state'),'reason':row.get('reason'),'updated_at':row.get('updated_at')} for row in recent]}
