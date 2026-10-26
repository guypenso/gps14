"""Controlled 100-listing POC using the existing collector, cache, DB and verification engine.
Default invocation prints a request plan only, with no network or database connection.
"""
import argparse
import datetime as dt
import json
import os
import urllib.parse
from pathlib import Path
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
      retries=0,additional_pages=False,emails=False,isolated_database_required=True,fresh_usage_snapshot_required=True,
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
    candidates=[];rejected=0;missing=0;rejections=[];pending_initial=[];filter_counts={}
    # Reports are scoped to the persisted POC response, not all earlier scans in the DB.
    saved=store.execute('SELECT payload FROM settings WHERE name=?',('poc_rows:'+job_id,)).fetchone()
    identities=json.loads(saved[0]) if saved else [r[0] for r in store.execute('SELECT property_id FROM scan_tasks WHERE job_id=?',(job_id,)).fetchall()]
    for identity in identities:
        p=store.get_property(identity)
        if not p:continue
        row=p['payload'];basic,reasons=verification(row,boundary)
        detail=dict(id=identity,address=row.get('formattedAddress'),price=row.get('price'),reasons=reasons,listing_url=safe_listing_link(row))
        for name,codes in {'residential_type':{'non_residential'},'status':{'status'},'price':{'price'},'bedrooms':{'bedrooms'},'bathrooms':{'bathrooms'},'square_feet':{'squareFootage'},'zip':{'zipCode','excluded_zip'},'municipal_boundary':{'coordinates','outside_city_or_boundary','state'}}.items():
            filter_counts[name]=filter_counts.get(name,0)+int(not set(reasons)&codes)
        if basic=='Rejected':rejected+=1;rejections.append(detail);continue
        if basic!='Candidate':missing+=1;pending_initial.append(detail);continue
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
    return dict(filter_pass_counts=filter_counts,filter_counts_are_independent=True,rejections=rejections,pending_initial_properties=pending_initial,job_id=job_id,received=stats.get('received',0),total_matching_api_query=stats.get('total'),
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


class ProofClient(Client):
    """Persist conservative endpoint attempt slots, including crash/restart cases."""
    def get(self,path,params=None,cache=True):
        from .storage import BudgetExceeded
        group='listing' if path=='/listings/sale' else 'tax' if path.startswith('/properties/') else None
        if group is None:raise PermissionError('Endpoint outside bounded proof')
        key='proof_attempts:'+self.settings.approval_id+':'+group
        row=self.store.execute('SELECT payload FROM settings WHERE name=?',(key,)).fetchone()
        used=json.loads(row[0]) if row else 0
        if used>=(1 if group=='listing' else 3):raise BudgetExceeded('Proof endpoint attempt limit reached')
        # Caller holds the scan lock. Count before invoking transport; cache hits count conservatively.
        self.store.save_setting(key,used+1)
        return super().get(path,params,cache)


def validate_proof_target(url,args,now=None):
    """No production writes and no blind assumption that provider stops overages."""
    from .database import validate_postgres_url
    validate_postgres_url(url,allow_local=True)
    parts=urllib.parse.urlsplit(url)
    if not args.isolated or parts.hostname not in {'localhost','127.0.0.1','::1'} or not parts.path.endswith('_test'):
        raise PermissionError('Proof requires isolated localhost PostgreSQL database ending _test')
    now=now or dt.datetime.now(dt.timezone.utc)
    try:
        stamp=dt.datetime.fromisoformat(args.usage_as_of.replace('Z','+00:00'))
        renewal=dt.date.fromisoformat(args.renewal_date)
        valid=stamp.tzinfo is not None and dt.timedelta(0)<=now-stamp<=dt.timedelta(minutes=15) and now.date()<renewal
    except (AttributeError,ValueError,TypeError):valid=False
    if not valid or isinstance(args.provider_used,bool) or args.provider_used is None or not 0<=args.provider_used<=46:
        raise PermissionError('Need fresh confirmed usage with room for all4 requests and a future renewal date')


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute',action='store_true',help='Run only after explicit API allowance approval')
    parser.add_argument('--job-id',default='poc-100-listings',help='Reuse to resume the same controlled sample')
    parser.add_argument('--isolated',action='store_true',help='Required for live proof: localhost PostgreSQL database ending _test only')
    parser.add_argument('--provider-used',type=int)
    parser.add_argument('--usage-as-of',help='Fresh timezone-aware provider usage snapshot, no older than15 minutes')
    parser.add_argument('--renewal-date',help='Confirmed next billing date YYYY-MM-DD')
    parser.add_argument('--output',type=Path,default=Path('/tmp/rentalsourcing-real-proof'))
    args=parser.parse_args(argv)
    if not args.execute:print(json.dumps(plan(),ensure_ascii=False,indent=2));return
    validate_proof_target(os.getenv('DATABASE_URL',''),args)
    s=Settings.from_env()
    if not s.enabled or not s.approval_id or s.approval_mode!='one_time' or not 1<=s.approved_requests<=4:
        raise PermissionError('POC needs a dedicated approved allowance of 1..4 attempts')
    s=replace(s,page_size=100,max_pages=1,retries=0,per_run=min(4,s.per_run),email_enabled=False)
    boundary=load_boundary(s.boundary_file) # Fail before requesting listings if city verification is unavailable.
    url=os.environ['DATABASE_URL']
    if not url.startswith(('postgres://','postgresql://')):raise ValueError('POC uses existing PostgreSQL persistence')
    store=Store(url)
    try:
        if not store.lock():raise RuntimeError('A scan is already running')
        store.migrate();client=ProofClient(store,s)
        now=dt.datetime.now(dt.timezone.utc)
        day=now.date().isoformat()
        try:stats=PocEngine(store,boundary,client,s).run(args.job_id,day,candidate_limit=3,first_page_only=True)
        except Exception as exc:
            stats=dict(store.job(args.job_id,day)['stats'],discovery_state='failed',partial=True,error_type=type(exc).__name__)
            from .request import diagnose
            stats['failure']=diagnose(exc)
        result=report(store,boundary,args.job_id,stats)
        result['attempts_this_run']=client.count;result['cache_hits_this_run']=client.cache_hits
        result['attempts_under_approval']=store.count_usage('approval:'+s.approval_id)
        result['approved_attempts']=s.approved_requests
        from .digest import Digest
        preview_settings=replace(s,email_from='preview@example.invalid',email_to='owner@example.invalid')
        preview_id=Digest(store,boundary,preview_settings).prepare(args.job_id,day,stats,preview=True)
        body=json.loads(store.execute('SELECT payload FROM outbox WHERE id=?',(preview_id,)).fetchone()[0])
        result.update(proof_status='failed' if stats.get('error_type') else 'completed_with_pending' if stats.get('partial') else 'completed',fresh=stats.get('discovery_state')=='fresh',provider_snapshot_used=args.provider_used,provider_used_upper_bound=args.provider_used+result['attempts_under_approval'],no_real_emails=True)
        args.output.mkdir(parents=True,exist_ok=True)
        for ext,value in [('json',json.dumps(result,ensure_ascii=False,indent=2)),('html',body['html']),('txt',body['text'])]:(args.output/('report.'+ext)).write_text(value)
        print(json.dumps(result,ensure_ascii=False,indent=2))
    finally:store.close()

if __name__=='__main__':main()
