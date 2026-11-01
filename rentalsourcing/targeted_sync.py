"""Owner-authorized one-property recovery. No provider/email calls or migrations.

Run on the deployed cron, where the existing private sync credentials are set:
    python -m rentalsourcing.targeted_sync --execute
The dashboard Shell can use hidden prompts if those credentials are absent.
"""
import argparse
import datetime as dt
import getpass
import hashlib
import hmac
import json
import os
import re
import urllib.request

from .command_center import snapshot, configuration, signed_request
from .config import Settings
from .core import verification, tax_verification
from .collector import LIVE
from .database import validate_postgres_url
from .digest import recent_timestamp
from .geography import load_boundary
from .provenance import demo_marked
from .request import NoRedirect
from .storage import Store

SOURCE_ID='2106-Rockledge-St,-Pittsburgh,-PA-15212'
ORIGINAL='daily-53b8b3ea92b45f2bd130943a675ea1290721f605ae1f8acc2fe9d564b9ffbbda'
JOB='targeted-sync:rockledge:2026-10-28:v1'
IDENTITY='daily-'+hashlib.sha256(JOB.encode()).hexdigest()


def prepare(store,boundary,now):
    original=store.execute('SELECT state,payload FROM outbox WHERE id=?',(ORIGINAL,)).fetchone()
    if not original or original[0]!='sent':raise ValueError('Original accepted report unavailable')
    email=json.loads(original[1]);text=email.get('text','')
    pattern=r'(?m)^2106 Rockledge St, Pittsburgh, PA 15212 \| \$109000 \| 3 beds / 2 baths / 1232 sqft \| ZIP 15212 \| Status Active\nTax/year: not verified \| Missing: [^\n]+\nObserved: ([^\n|]+) \| Source: RentCast(?:\n|$)'
    matches=list(re.finditer(pattern,text))
    if len(matches)!=1:raise ValueError('Original selected card could not be independently verified')
    p=store.get_property(SOURCE_ID)
    if not p or p['source']!=LIVE:raise ValueError('Live source identity unavailable')
    row=p['payload']
    if row.get('id')!=SOURCE_ID or demo_marked(row):raise ValueError('Source identity mismatch')
    state,reasons=verification(row,boundary)
    if state=='Rejected' or boundary.evaluate(row)[0]!='Candidate':raise ValueError('Current mandatory filters fail: '+','.join(reasons))
    observed=p['observed_at'];cached=store.get_tax(SOURCE_ID,now.isoformat());tax=None
    if cached:
        record,source,fetched=cached
        if source!=LIVE or not isinstance(record,dict) or record.get('id')!=SOURCE_ID or demo_marked(record):raise ValueError('Cached tax identity conflict')
        tax_state,tax,tax_reason=tax_verification(record,now.date())
        if tax_state=='Rejected':raise ValueError('Current tax hard filter fails: '+str(tax_reason))
    else:fetched=None
    if not recent_timestamp(observed,now):reasons.append('stale_listing_requires_current_verification')
    if not tax:reasons.append('tax_not_verified')
    qualified=state=='Candidate' and bool(tax) and recent_timestamp(observed,now)
    item=dict(listing=row,tax=tax,listing_observed_at=observed,reasons=list(dict.fromkeys(reasons)),tax_fetched_at=fetched)
    if qualified:item['kind']='new';item['verified_at']=observed
    else:item['kind']='pending_verification'
    # Reuse the exact production serializer without creating a fake email/outbox.
    class Capture:
        payload=None
        def execute(self,sql,args=()):
            if sql.startswith('INSERT INTO command_center_reports'):self.payload=args[1];return None
            return store.execute(sql,args)
    capture=Capture()
    snapshot(capture,IDENTITY,'2026-10-28',[item] if qualified else [],
             {'discovery_state':'stored','pending_properties':[] if qualified else [item]},now,boundary)
    report=json.loads(capture.payload)
    if len(report['opportunities'])!=1 or report['opportunities'][0]['source_id']!=SOURCE_ID:raise ValueError('Target selection is not exactly one original property')
    return capture.payload,state,reasons


def run(store,boundary,now,execute=False,config=None,opener=None):
    if not store.lock():raise ValueError('Another production job holds the existing application lock')
    # Freeze once before transport: retries use identical bytes/report ID.
    saved=store.execute('SELECT payload FROM settings WHERE name=?',(JOB,)).fetchone()
    current,state,reasons=prepare(store,boundary,now)
    ledger=json.loads(saved[0]) if saved else {'payload':current,'attempts':0,'synced_at':None,'error_type':None}
    if ledger['synced_at']:
        print('TARGET_ALREADY_ACCEPTED report_id='+IDENTITY);return
    if saved:
        previous=json.loads(ledger['payload'])['opportunities'][0]
        if previous!=json.loads(current)['opportunities'][0]:
            raise ValueError('Stored listing changed since frozen attempt; review before retry')
    card=json.loads(ledger['payload'])['opportunities'][0]
    print('TARGET_DRY_RUN_VERIFIED count=1 qualification='+card['qualification']+' tax_status='+card['tax_status']+' report_id='+IDENTITY)
    if not execute:return
    if not config:raise ValueError('Private synchronization configuration unavailable')
    body=ledger['payload'].encode()
    ledger['attempts']+=1;store.save_setting(JOB,ledger)
    request=signed_request(config,body,now)
    try:
        with (opener or urllib.request.build_opener(NoRedirect)).open(request,timeout=20) as response:
            receipt=json.loads(response.read(4096))
            if response.status!=200 or receipt.get('accepted') is not True or receipt.get('report_id')!=IDENTITY:raise ValueError('Invalid receiver receipt')
        ledger['synced_at']=now.isoformat();ledger['error_type']=None;store.save_setting(JOB,ledger)
        # Reevaluate this existing record only, without rewriting history or outbox.
        store.set_verification(SOURCE_ID,'Qualified' if card['qualification']=='verified' else 'Pending Verification',reasons)
        print('TARGET_SYNC_ACCEPTED count=1 report_id='+IDENTITY+' duplicate='+str(receipt.get('duplicate',False)).lower())
    except Exception as exc:
        category='unknown'
        if hasattr(exc,'read'):
            try:
                error=json.loads(exc.read(4096)).get('error','')
                if error=='Invalid synchronization authentication':category='receiver_hmac_or_timestamp'
                elif getattr(exc,'code',None) in {401,403}:category='gateway_or_unrecognized_authentication'
            except Exception:category='gateway_or_non_json_response'
        ledger['error_type']=type(exc).__name__;store.save_setting(JOB,ledger)
        raise ValueError('Synchronization failed: '+type(exc).__name__+(' HTTP '+str(exc.code) if hasattr(exc,'code') else '')+' category='+category) from None


def verify_auth(config,now,opener=None):
    """Signed invalid-schema probe: receiver rejects before any database write."""
    import urllib.error
    request=signed_request(config,b'{}',now)
    try:
        with (opener or urllib.request.build_opener(NoRedirect)).open(request,timeout=20):pass
    except urllib.error.HTTPError as exc:
        try:error=json.loads(exc.read(4096)).get('error','')
        except Exception:error=''
        if exc.code==400 and error=='Unsupported report':
            print('SYNC_AUTHENTICATION_VERIFIED');return
        if exc.code==401 and error=='Invalid synchronization authentication':
            raise ValueError('Receiver rejected HMAC/timestamp: use the existing cron signing secret, not the Sites service token; credentials were not changed') from None
        raise ValueError('Authentication probe rejected: HTTP '+str(exc.code)+' '+('receiver' if error else 'gateway_or_non_json')) from None
    raise ValueError('Unexpected authentication probe response')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--execute',action='store_true');args=parser.parse_args()
    store=None
    try:
        settings=Settings.from_env();url=validate_postgres_url(os.getenv('DATABASE_URL',''))
        config=None
        if args.execute:
            # Dashboard does not hold cron-only secrets. Never echo or log them.
            for key in ('COMMAND_CENTER_SYNC_SECRET','COMMAND_CENTER_SERVICE_TOKEN'):
                if not os.getenv(key):os.environ[key]=getpass.getpass(key+' (hidden): ').strip().strip('\"').strip("'")
            os.environ['ENABLE_COMMAND_CENTER_SYNC']='true'
            if not os.getenv('COMMAND_CENTER_SYNC_URL'):
                os.environ['COMMAND_CENTER_SYNC_URL']='https://inspection-command-windows-20261017.roundmesa2.chatgpt.site/api/rentalsourcing/sync'
            config=configuration()
            verify_auth(config,dt.datetime.now(dt.timezone.utc))
        store=Store(url,initialize=False)
        run(store,load_boundary(settings.boundary_file),dt.datetime.now(dt.timezone.utc),args.execute,config)
    except Exception as exc:
        print('STOP: '+(str(exc) if isinstance(exc,ValueError) else type(exc).__name__));raise SystemExit(1)
    finally:
        if store:store.close()

if __name__=='__main__':main()
