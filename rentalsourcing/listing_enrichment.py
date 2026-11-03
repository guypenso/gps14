"""Stored listing metadata only. No provider, email, browser, or paid API calls."""
import datetime as dt,hashlib,json,os,urllib.request
from .command_center import review_configuration,signed_request
from .database import validate_postgres_url
from .storage import Store
from .request import NoRedirect

JOB='listing-contact-enrichment:2026-11-03:neighborhoods-condominium-v2'

def evidence(p,observed):
    a=p.get('listingAgent');office=p.get('listingOffice');agents=[]
    if isinstance(a,dict):
        agents=[dict(role='listing_agent',name=a.get('name'),email=a.get('email'),phone=a.get('phone'),brokerage=office.get('name') if isinstance(office,dict) else None)]
    def bounded(value,limit):return value[:limit] if isinstance(value,str) else None
    for a in agents:
        for field,limit in [('name',200),('email',254),('phone',60),('brokerage',200)]:a[field]=bounded(a[field],limit)
    from .turnkey import evidence as turnkey_evidence
    from .condominium import evidence as condo_evidence
    return dict(condominium_evidence=condo_evidence(p),latitude=p.get('latitude'),longitude=p.get('longitude'),turnkey_evidence=turnkey_evidence(p),source='RentCast',source_id=p.get('id'),address=p.get('formattedAddress'),street_address=p.get('addressLine1'),unit=p.get('addressLine2'),city=p.get('city'),state=p.get('state'),zip_code=p.get('zipCode'),retrieved_at=observed,zillow_url=p.get('zillowUrl') or p.get('listingUrl'),agents=agents,listing_status=p.get('status'))

def run(store,config,*,job_key=JOB,opener=None):
    if not config:return {'state':'disabled'}
    if not store.lock():raise ValueError('Existing job is busy')
    saved=store.execute('SELECT payload FROM settings WHERE name=?',(job_key,)).fetchone()
    ledger=json.loads(saved[0]) if saved else {'offset':0,'cursor':0,'done':False,'batches':0}
    if ledger['done']:print('LISTING_ENRICHMENT_ALREADY_ACCEPTED '+json.dumps(ledger.get('summary',{})));return ledger
    url,key,token=config;url=url.rsplit('/',1)[0]+'/review-listing-enrichment';opener=opener or urllib.request.build_opener(NoRedirect)
    # Source labels vary by maintained collector; accept only the exact configured LIVE value.
    from .collector import LIVE
    rows=store.execute('SELECT id,payload,observed_at FROM properties WHERE source=? ORDER BY id',(LIVE,)).fetchall()
    for _ in range(100):
        records=[evidence(json.loads(raw),observed) for identity,raw,observed in rows[ledger['offset']:ledger['offset']+100]]
        body=json.dumps({'cursor':ledger['cursor'] or 0,'records':records},ensure_ascii=False,separators=(',',':')).encode();now=dt.datetime.now(dt.timezone.utc)
        from .sync_runtime import request_json
        def validate(result,*args):
            if result.get('accepted') is not True:raise ValueError('Invalid listing enrichment receipt')
        result=request_json(store,'listing_enrichment',job_key+':'+str(ledger['offset'])+':'+str(ledger['cursor']),(url,key,token),body,timeout=90,max_bytes=20000,opener=opener,now=now,validate=validate)
        ledger['offset']+=len(records);ledger['cursor']=result.get('next_cursor');ledger['batches']+=1;ledger['summary']=result.get('summary',{})
        ledger['done']=ledger['offset']>=len(rows) and ledger['cursor'] is None
        store.save_setting(job_key,ledger)
        if ledger['done']:
            from .turnkey import evidence as policy,FIELDS
            source=[json.loads(row[1]) for row in rows]
            print('LISTING_ENRICHMENT_ACCEPTED '+json.dumps(dict(source_records=len(rows),source_turnkey=sum(policy(p)['classified'] for p in source),source_marketing_records=sum(any(isinstance(p.get(k),str) and p[k].strip() for k in FIELDS if k not in ('propertyType','listingType','propertySubType','propertySubtype')) for p in source),**ledger['summary']),sort_keys=True));return ledger
        # Once initialization is complete, do not initialize the same rows again.
        if ledger['cursor'] is None:ledger['cursor']=10000
    raise ValueError('Enrichment batch limit reached; progress preserved')

def main():
    store=None
    try:
        config=review_configuration()
        if not config:raise ValueError('Existing private review configuration required')
        store=Store(validate_postgres_url(os.getenv('DATABASE_URL','')),initialize=False);run(store,config)
    except Exception as exc:print('LISTING_ENRICHMENT_DEFERRED '+type(exc).__name__);raise SystemExit(1)
    finally:
        if store:store.close()
if __name__=='__main__':main()
