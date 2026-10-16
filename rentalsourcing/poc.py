"""Controlled 100-listing POC using the existing collector, cache, DB and verification engine.
Default invocation prints a request plan only, with no network or database connection.
"""
import argparse
import datetime as dt
import json
import os
import urllib.parse
from dataclasses import replace
from .config import Settings
from .request import PARAMS
from .collector import Client,LIVE
from .engine import Engine
from .storage import Store
from .geography import load_boundary
from .core import verification,tax_verification

PARAMETERS=dict(PARAMS,limit=100,offset=0)

def plan():
    return dict(mode='plan_only_no_requests',method='GET',
      url='https://api.rentcast.io/v1/listings/sale?'+urllib.parse.urlencode(sorted(PARAMETERS.items())),
      parameters=PARAMETERS,max_listing_requests=1,max_tax_candidates=3,max_total_attempts=4,
      retries=0,additional_pages=False,emails=False,
      tax_endpoint='/v1/properties/{URL-encoded RentCast property id}',
      required_approval='Explicit user approval of up to four attempts; configure a dedicated API_APPROVAL_ID and APPROVED_API_REQUESTS only after approval.')


def safe_listing_link(row):
    value=row.get('listingUrl')
    if not isinstance(value,str):return None
    try:
        u=urllib.parse.urlsplit(value)
        return value if u.scheme=='https' and u.hostname and not u.username and not u.password else None
    except ValueError:return None


def report(store,boundary,job_id,stats,clock=None):
    now=(clock or (lambda:dt.datetime.now(dt.timezone.utc)))()
    candidates=[];rejected=0;missing=0
    # Reports are scoped to the persisted POC response, not all earlier scans in the DB.
    saved=store.execute('SELECT payload FROM settings WHERE name=?',('poc_rows:'+job_id,)).fetchone()
    identities=json.loads(saved[0]) if saved else [r[0] for r in store.execute('SELECT property_id FROM scan_tasks WHERE job_id=?',(job_id,)).fetchall()]
    for identity in identities:
        p=store.get_property(identity)
        if not p:continue
        row=p['payload'];basic,reasons=verification(row,boundary)
        if basic=='Rejected':rejected+=1;continue
        if basic!='Candidate':missing+=1;continue
        cached=store.get_tax(identity,now.isoformat());tax=None;tax_source=None
        if cached and cached[1]==LIVE and cached[0].get('id')==identity:
            _,tax,_=tax_verification(cached[0],now.date());tax_source='RentCast Property Records'
        state=p['state'];report_reasons=list(p['reasons'])
        stale=now-dt.datetime.fromisoformat(p['observed_at'])>dt.timedelta(hours=24)
        if state=='Qualified' and (tax is None or stale):
            state='Pending Verification';report_reasons.append('stale_listing' if stale else 'tax_cache_expired')
        candidates.append(dict(id=identity,address=row.get('formattedAddress'),price=row.get('price'),
          bedrooms=row.get('bedrooms'),bathrooms=row.get('bathrooms'),squareFootage=row.get('squareFootage'),zipCode=row.get('zipCode'),
          state=state,reasons=report_reasons,listing_observed_at=p['observed_at'],tax_fetched_at=cached[2] if cached else None,listing_source='RentCast Sale Listings',tax_source=tax_source,
          tax_year=tax['year'] if tax else None,annual_tax=tax['annual'] if tax else None,
          adjusted_annual_tax=tax['adjusted'] if tax else None,listing_url=safe_listing_link(row)))
    return dict(job_id=job_id,received=stats.get('received',0),total_matching_api_query=stats.get('total'),
      initial_pass_count=len(candidates),rejected_initially=rejected,pending_missing_initial_fields=missing,
      qualified_in_sample=sum(r['state']=='Qualified' for r in candidates),sample_only=True,
      tax_candidates_limit=3,stats=stats,properties=candidates,coverage={'state':'Not measured','value':None,'reason':'No authorized comparison sample has been evaluated.'})


class PocEngine(Engine):
    def ingest(self,job_id,row):
        super().ingest(job_id,row)
        # Capture all raw response identities, including rejected/missing-field rows.
        key='poc_rows:'+job_id
        saved=self.store.execute('SELECT payload FROM settings WHERE name=?',(key,)).fetchone()
        ids=json.loads(saved[0]) if saved else []
        if isinstance(row,dict):
            import hashlib
            identity=row.get('id')
            if not isinstance(identity,str) or not identity:
                identity='unidentified:'+hashlib.sha256(json.dumps(row,sort_keys=True).encode()).hexdigest()
            if identity not in ids:ids.append(identity)
        self.store.save_setting(key,ids)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute',action='store_true',help='Run only after explicit API allowance approval')
    parser.add_argument('--job-id',default='poc-100-listings',help='Reuse to resume the same controlled sample')
    args=parser.parse_args(argv)
    if not args.execute:print(json.dumps(plan(),ensure_ascii=False,indent=2));return
    s=Settings.from_env()
    if not s.enabled or not s.approval_id or not 1<=s.approved_requests<=4:
        raise PermissionError('POC needs a dedicated approved allowance of 1..4 attempts')
    s=replace(s,page_size=100,max_pages=1,retries=0,per_run=min(4,s.per_run),email_enabled=False)
    boundary=load_boundary(s.boundary_file) # Fail before requesting listings if city verification is unavailable.
    url=os.environ['DATABASE_URL']
    if not url.startswith(('postgres://','postgresql://')):raise ValueError('POC uses existing PostgreSQL persistence')
    store=Store(url)
    try:
        if not store.lock():raise RuntimeError('A scan is already running')
        store.migrate();client=Client(store,s)
        stats=PocEngine(store,boundary,client,s).run(args.job_id,dt.date.today().isoformat(),candidate_limit=3,first_page_only=True)
        result=report(store,boundary,args.job_id,stats)
        result['attempts_this_run']=client.count;result['cache_hits_this_run']=client.cache_hits
        print(json.dumps(result,ensure_ascii=False,indent=2))
    finally:store.close()

if __name__=='__main__':main()
