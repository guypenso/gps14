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
    from .property_types import property_type_evidence,type_fields
    from .turnkey import evidence as turnkey_evidence
    from .condominium import evidence as condo_evidence
    fields=('heatingType', 'coolingType', 'garageSpaces', 'parkingSpaces', 'hasGarage', 'hasBasement', 'hasLaundry', 'garageType', 'parkingType', 'basementType', 'roofType', 'roofMaterial', 'roofReplacementYear', 'hvacReplacementYear', 'furnaceReplacementYear', 'waterHeaterReplacementYear')
    features=p.get('features') if isinstance(p.get('features'),dict) else {}
    facts={k:p.get(k,features.get(k)) for k in fields if isinstance(p.get(k,features.get(k)),(str,int,float,bool))}
    for target,alias in [('hasGarage','garage'),('hasBasement','basement'),('hasLaundry','laundry')]:
        if target not in facts and isinstance(features.get(alias),bool):facts[target]=features[alias]
    for k in ('currentMonthlyRent','existingRent','actualRent'):
        v=p.get(k)
        if isinstance(v,(float,int)) and not isinstance(v,bool) and 0<v<100000:facts['documented_monthly_rent']=v;break
    description=next((p[k][:12000] for k in ('description','listingDescription','publicRemarks','remarks') if isinstance(p.get(k),str) and p[k].strip()),'')
    return dict(property_type_evidence=property_type_evidence(p),property_type_fields=type_fields(p),description=description,insight_facts=facts,condominium_evidence=condo_evidence(p),latitude=p.get('latitude'),longitude=p.get('longitude'),turnkey_evidence=turnkey_evidence(p),source='RentCast',source_id=p.get('id'),address=p.get('formattedAddress'),street_address=p.get('addressLine1'),unit=p.get('addressLine2'),city=p.get('city'),state=p.get('state'),zip_code=p.get('zipCode'),retrieved_at=observed,zillow_url=p.get('zillowUrl') or p.get('listingUrl'),agents=agents,listing_status=p.get('status'))

def stored_evidence(store,p,observed,now=None):
    """Reuse only matching, unexpired live property records. Never performs a lookup."""
    from .collector import LIVE
    from .provenance import demo_marked
    out=evidence(p,observed)
    out['insight_facts_provenance']={k:{'source':'RentCast stored listing','observed_at':observed} for k in out['insight_facts']}
    saved=store.get_tax(p.get('id'),(now or dt.datetime.now(dt.timezone.utc)).isoformat())
    from .rental_intelligence import cached_estimate
    rent=cached_estimate(store,p,now or dt.datetime.now(dt.timezone.utc))
    if rent:out['rent_estimate']=rent
    if not saved:return out
    record=json.loads(saved[0]) if isinstance(saved[0],str) else saved[0]
    if saved[1]!=LIVE or not isinstance(record,dict) or record.get('id')!=p.get('id') or demo_marked(record):return out
    from .tax_intelligence import provider_evidence
    history=store.execute('SELECT payload,source,fetched_at,expires_at FROM tax_history WHERE property_id=?',(p.get('id'),)).fetchone()
    tax=provider_evidence(p.get('id'),history,now or dt.datetime.now(dt.timezone.utc))
    if tax and tax['state']=='provider_verified':out['provider_tax']=dict(tax,source_id=p.get('id'))
    cached=evidence(record,saved[2])
    if not out['agents'] and cached['agents']:out['agents']=cached['agents']
    if not out['description'] and cached['description']:
        out['description']=cached['description'];out['description_source']='RentCast cached property record';out['description_observed_at']=saved[2]
    if cached['property_type_evidence']['evidence']:
        combined=dict(p);combined['propertyDetails']=record
        out['property_type_evidence']=__import__('rentalsourcing.property_types',fromlist=['property_type_evidence']).property_type_evidence(combined)
    for k,v in cached['insight_facts'].items():
        if k not in out['insight_facts']:
            out['insight_facts'][k]=v
            out['insight_facts_provenance'][k]={'source':'RentCast cached property record','observed_at':saved[2]}
    return out

def research_context(store):
    from .config import Settings
    settings=Settings.from_env();at=dt.datetime.now(dt.timezone.utc)
    cached=store.execute('SELECT payload FROM settings WHERE name=?',('research_budget_context',)).fetchone()
    if not settings.enabled and cached:return json.loads(cached[0])
    scheduled=store.execute('SELECT payload FROM settings WHERE name=?',('health_budget',)).fetchone()
    budget=store.budget_health(settings,at,record_warning=False) if settings.enabled else json.loads(scheduled[0]) if scheduled else {'remaining':0}
    value={'remaining':budget.get('remaining',0),'approval_cap':settings.request_ceiling if settings.enabled else budget.get('approval_cap',27),'approval_id':settings.approval_id,'approval_mode':settings.approval_mode,'rent_authorized':settings.enabled and settings.approval_mode=='monthly_budget' and os.getenv('ENABLE_RENT_ESTIMATES','false').lower()=='true','observed_at':at.isoformat()}
    if settings.enabled:store.save_setting('research_budget_context',value)
    return value

def run(store,config,*,job_key=JOB,opener=None,acquire_lock=True):
    if not config:return {'state':'disabled'}
    if acquire_lock and not store.lock():raise ValueError('Existing job is busy')
    saved=store.execute('SELECT payload FROM settings WHERE name=?',(job_key,)).fetchone()
    ledger=json.loads(saved[0]) if saved else {'offset':0,'cursor':0,'done':False,'batches':0}
    if ledger['done']:print('LISTING_ENRICHMENT_ALREADY_ACCEPTED '+json.dumps(ledger.get('summary',{})));return ledger
    url,key,token=config;url=url.rsplit('/',1)[0]+'/review-listing-enrichment';opener=opener or urllib.request.build_opener(NoRedirect)
    # Source labels vary by maintained collector; accept only the exact configured LIVE value.
    from .collector import LIVE
    rows=store.execute('SELECT id,payload,observed_at FROM properties WHERE source=? ORDER BY id',(LIVE,)).fetchall()
    from .owner_exception import snapshot as exception_snapshot
    owner_snapshot=exception_snapshot(store,dt.datetime.now(dt.timezone.utc))
    for _ in range(100):
        records=[stored_evidence(store,json.loads(raw),observed) for identity,raw,observed in rows[ledger['offset']:ledger['offset']+25]]
        body=json.dumps({'cursor':ledger['cursor'] or 0,'records':records,'owner_exception_source':owner_snapshot,'research_context':research_context(store)},ensure_ascii=False,separators=(',',':')).encode();now=dt.datetime.now(dt.timezone.utc)
        from .sync_runtime import request_json
        def validate(result,*args):
            if result.get('accepted') is not True:raise ValueError('Invalid listing enrichment receipt')
        result=request_json(store,'listing_enrichment',job_key+':'+str(ledger['offset'])+':'+str(ledger['cursor']),(url,key,token),body,timeout=90,max_bytes=524288,opener=opener,now=now,validate=validate)
        ledger['offset']+=len(records);ledger['cursor']=result.get('next_cursor');ledger['batches']+=1;ledger['summary']=result.get('summary',{})
        ledger['done']=ledger['offset']>=len(rows) and ledger['cursor'] is None
        if result.get('summary',{}).get('property_quality'):store.save_setting('property_quality:last',result['summary']['property_quality'])
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


INSIGHTS_JOB='listing-contact-enrichment:2026-11-12:continuous-research-v1'
def initial_insights(store,config=None):
    """Read-only source snapshot, separately locked from paid discovery/recovery."""
    config=config or review_configuration()
    if not config:return {'state':'disabled'}
    saved=store.execute('SELECT payload FROM settings WHERE name=?',(INSIGHTS_JOB,)).fetchone()
    if saved and json.loads(saved[0]).get('done'):return {'state':'complete'}
    if store.pg and not store.execute('SELECT pg_try_advisory_lock(73541213)').fetchone()[0]:return {'state':'busy'}
    try:
        from .criteria import reevaluate
        from .config import Settings
        from .geography import load_boundary
        key='property_quality:source_reconciliation:v1'
        if not store.execute('SELECT payload FROM settings WHERE name=?',(key,)).fetchone():
            changed=reevaluate(store,load_boundary(Settings.from_env().boundary_file),dt.datetime.now(dt.timezone.utc))
            store.save_setting(key,{'at':dt.datetime.now(dt.timezone.utc).isoformat(),'records_updated':changed,'provider_calls':0,'manual_decisions_preserved':True})
            print('PROPERTY_QUALITY_SOURCE_RECONCILIATION '+json.dumps({'records_updated':changed,'provider_calls':0}))
        return run(store,config,job_key=INSIGHTS_JOB,acquire_lock=False)
    finally:
        if store.pg:store.execute('SELECT pg_advisory_unlock(73541213)')
