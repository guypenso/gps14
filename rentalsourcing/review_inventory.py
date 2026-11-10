"""Durable stored-only review backfill. No RentCast, email, schema or schedule changes."""
import argparse,datetime as dt,getpass,hashlib,json,os,urllib.request
from .config import Settings
from .storage import Store
from .database import validate_postgres_url
from .geography import load_boundary
from .core import verification,tax_verification
from .collector import LIVE
from .tax_backlog import review_deferred,previous_price
from .digest import listing_link,recent_timestamp
from .command_center import configuration,signed_request,review_configuration
from .request import NoRedirect
from .provenance import demo_marked
from .review_feedback import refresh
from .public_tax import download_and_match,unknown

JOB='stored-review-inventory:2026-11-02:turnkey-v1'


def select(store,boundary,now,*,source_ids=None,include_verified=False):
    cards=[];counts={'stored':0,'hard_rejected':0,'other_missing':0,'owner_rejected':0,'tax_rejected':0,'verified_tax':0,'actionable':0,'stale':0,'fresh':0}
    for identity,raw,source,observed in store.execute('SELECT id,payload,source,observed_at FROM properties').fetchall():
        if source_ids is not None and identity not in source_ids:continue
        counts['stored']+=1;p=json.loads(raw);state,reasons=verification(p,boundary)
        verified_tax=None;verified_fetched=None
        if state=='Rejected':counts['hard_rejected']+=1;continue
        if source!=LIVE or state!='Candidate':counts['other_missing']+=1;continue
        if review_deferred(identity,store):counts['owner_rejected']+=1;continue
        cached=store.get_tax(identity,now.isoformat())
        valid=cached and cached[1]==LIVE and isinstance(cached[0],dict) and cached[0].get('id')==identity and not demo_marked(cached[0])
        if valid:
            tax_state,tax,reason=tax_verification(cached[0],now.date())
            if tax_state=='Rejected':counts['tax_rejected']+=1;continue
            if tax_state=='Qualified':
                counts['verified_tax']+=1
                if not include_verified:continue
                verified_tax=tax;verified_fetched=cached[2]
        fresh=recent_timestamp(observed,now);counts['fresh' if fresh else 'stale']+=1
        first=store.execute('SELECT MIN(observed_at) FROM price_history WHERE property_id=?',(identity,)).fetchone()[0] or observed
        muni=boundary.municipality(p) if hasattr(boundary,'municipality') else 'Bellevue Borough' if boundary.bellevue.contains(p) else 'Pittsburgh'
        neighborhood='Brighton Heights' if boundary.brighton.contains(p) else 'Spring Hill-City View' if boundary.spring_hill.contains(p) else None
        from .condominium import evidence as condo_evidence
        card=dict(condominium_evidence=condo_evidence(p),latitude=p.get('latitude'),longitude=p.get('longitude'),source_id=identity,source='RentCast',source_label='RentCast Sale Listings',address=p.get('formattedAddress'),street_address=p.get('addressLine1'),unit=p.get('addressLine2'),city=p.get('city'),state=p.get('state'),zip_code=p.get('zipCode'),price=p.get('price'),bedrooms=p.get('bedrooms'),bathrooms=p.get('bathrooms'),square_feet=p.get('squareFootage'),annual_tax=None,tax_year=None,tax_status='pending_verification',qualification='pending_verification',section='pending_verification',previous_price=previous_price(store,identity,p.get('price')),listing_url=listing_link(p),reasons=['tax_verification_required']+([] if fresh else ['stale_listing_status_requires_refresh']),discovered_at=first,updated_at=observed,listing_observed_at=observed,verified_at=None,tax_fetched_at=None,last_reported_status=p.get('status'),municipality=muni,neighborhood=neighborhood)
        if verified_tax and fresh:
            card.update(annual_tax=verified_tax['annual'],tax_year=verified_tax['year'],tax_status='provider_verified',tax_source='RentCast Property Records',qualification='verified',section='new',reasons=[],verified_at=now.isoformat(),tax_fetched_at=verified_fetched)
        from .listing_enrichment import evidence
        from .rental_intelligence import cached_estimate
        card['rent_estimate']=cached_estimate(store,p,now)
        card['eligibility_path']=verified_tax.get('eligibility_path') if verified_tax and fresh else None
        card['listing_info']=evidence(p,observed)
        cards.append((card,p));counts['actionable']+=1
    return cards,counts


def run(store,boundary,now,*,execute=False,download=False,config=None,opener=None,job_key=JOB,refresh_decisions=True,source_ids=None,include_verified=False):
    opener=opener or urllib.request.build_opener(NoRedirect)
    if not store.lock():raise ValueError('Existing application job is busy; no backfill performed')
    saved=store.execute('SELECT payload FROM settings WHERE name=?',(job_key,)).fetchone()
    if execute and saved and json.loads(saved[0]).get('synced_at'):
        print('REVIEW_INVENTORY_ALREADY_ACCEPTED');return json.loads(saved[0])
    if execute and refresh_decisions:print('PENTEN_REVIEW_FEEDBACK '+json.dumps(refresh(store,now,opener,config),sort_keys=True))
    pairs,counts=select(store,boundary,now,source_ids=source_ids,include_verified=include_verified)
    print('STORED_REVIEW_DRY_RUN '+json.dumps(counts,sort_keys=True))
    if not execute:return counts
    if not config:raise ValueError('Existing private synchronization configuration required')
    saved=store.execute('SELECT payload FROM settings WHERE name=?',(job_key,)).fetchone()
    if saved:ledger=json.loads(saved[0])
    else:
        estimates={};manifest=None;warning='Public assessment batch not yet retrieved'
        cached=store.execute('SELECT payload FROM settings WHERE name=?',('public_tax_batch:2026',)).fetchone()
        if cached:
            cached=json.loads(cached[0]);estimates=cached['estimates'];manifest=cached['manifest']
        elif download:
            try:
                estimates,manifest=download_and_match([p for card,p in pairs],boundary,now)
                store.save_setting('public_tax_batch:2026',dict(estimates=estimates,manifest=manifest))
            except Exception as exc:warning='Public dataset unavailable: '+type(exc).__name__;print('PUBLIC_TAX_BATCH_UNAVAILABLE '+type(exc).__name__)
        cards=[]
        for card,p in pairs:
            card['tax_estimate']=estimates.get(card['source_id']) or unknown(now,warning)
            cards.append(card)
        batches=[]
        for offset in range(0,len(cards),125):
            part=offset//125
            body=json.dumps(dict(schema_version=1,report_id='daily-'+hashlib.sha256((job_key+':part:'+str(part)).encode()).hexdigest(),report_date=now.date().isoformat(),prepared_at=now.isoformat(),report_kind='stored_review',discovery_state='stored',opportunities=cards[offset:offset+125]),ensure_ascii=False,separators=(',',':'))
            batches.append(dict(payload=body,attempts=0,synced_at=None,error_type=None))
        ledger=dict(batches=batches,counts=counts,synced_at=None,public_manifest=manifest,delivery_scope='daily' if include_verified else 'review')
        store.save_setting(job_key,ledger)
    aggregate={}
    for batch in ledger['batches']:
        if batch.get('synced_at'):
            for key,value in batch.get('receipt',{}).get('counts',{}).items():aggregate[key]=aggregate.get(key,0)+value
            continue
        batch['attempts']+=1;store.save_setting(job_key,ledger)
        try:
            for card in json.loads(batch['payload'])['opportunities']:
                current=store.get_property(card['source_id'])
                if not current or verification(current['payload'],boundary)[0]!='Candidate':raise ValueError('Frozen candidate no longer passes non-tax rules; no import performed')
                if card.get('qualification')=='verified':
                    saved_tax=store.get_tax(card['source_id'],now.isoformat())
                    if not saved_tax or saved_tax[1]!=LIVE or saved_tax[0].get('id')!=card['source_id'] or tax_verification(saved_tax[0],now.date())[0]!='Qualified' or not recent_timestamp(current['observed_at'],now):
                        raise ValueError('Frozen candidate no longer passes verified freshness requirements; no import performed')
            from .sync_runtime import request_json
            report_id=json.loads(batch['payload'])['report_id']
            def validate(receipt,*args):
                if receipt.get('accepted') is not True or receipt.get('report_id')!=report_id:raise ValueError('Invalid inventory receipt')
            receipt=request_json(store,'inventory_sync',report_id,config,batch['payload'].encode(),timeout=60,max_bytes=16384,opener=opener,now=now,validate=validate)
            batch.update(synced_at=now.isoformat(),error_type=None,receipt=receipt);store.save_setting(job_key,ledger)
            for key,value in receipt.get('counts',{}).items():aggregate[key]=aggregate.get(key,0)+value
        except Exception as exc:
            batch['error_type']=type(exc).__name__;store.save_setting(job_key,ledger)
            raise ValueError('Stored review delivery failed: '+type(exc).__name__+(' HTTP '+str(getattr(exc,'http_status',getattr(exc,'code',None))) if getattr(exc,'http_status',getattr(exc,'code',None)) is not None else '')) from None
    ledger['synced_at']=now.isoformat();ledger['receipt_counts']=aggregate;store.save_setting(job_key,ledger)
    all_cards=[p for b in ledger['batches'] for p in json.loads(b['payload'])['opportunities']]
    print('REVIEW_INVENTORY_ACCEPTED '+json.dumps(aggregate,sort_keys=True))
    print('TAX_ESTIMATE_COUNTS '+json.dumps({'estimated':sum(p.get('tax_estimate',{}).get('state')=='estimated' for p in all_cards),'unknown':sum(p.get('tax_estimate',{}).get('state')!='estimated' for p in all_cards),'matched':sum(p.get('tax_estimate',{}).get('match_status')=='unique_parcel' for p in all_cards)}))
    return ledger


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--unattended',action='store_true');parser.add_argument('--execute',action='store_true');parser.add_argument('--download-public-data',action='store_true');args=parser.parse_args();store=None
    try:
        config=review_configuration()
        if args.execute and not config:
            if args.unattended:raise ValueError('Private review configuration required for unattended stored-data job')
            for key in ['COMMAND_CENTER_SYNC_SECRET','COMMAND_CENTER_SERVICE_TOKEN']:
                if not os.getenv(key):os.environ[key]=getpass.getpass(key+' (hidden): ').strip().strip('\"').strip("'")
            os.environ['ENABLE_COMMAND_CENTER_SYNC']='true'
            if not os.getenv('COMMAND_CENTER_SYNC_URL'):os.environ['COMMAND_CENTER_SYNC_URL']='https://inspection-command-windows-20261017.roundmesa2.chatgpt.site/api/rentalsourcing/sync'
            config=configuration()
        store=Store(validate_postgres_url(os.getenv('DATABASE_URL','')),initialize=False)
        run(store,load_boundary(Settings.from_env().boundary_file),dt.datetime.now(dt.timezone.utc),execute=args.execute,download=args.download_public_data,config=config)
    except Exception as exc:
        print('STOP: '+(str(exc) if isinstance(exc,ValueError) else type(exc).__name__));raise SystemExit(1)
    finally:
        if store:store.close()

if __name__=='__main__':main()
