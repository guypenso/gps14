"""Selective address/ID property-record enrichment. No sale-listing or AVM discovery."""
import datetime as dt
import json
from urllib.parse import quote
from .collector import Client,LIVE,tax_expiry
from .core import verification
from .digest import recent_timestamp
from .provenance import demo_marked
from .realscout import addresses,address_key
from .realscout_store import cached_identity_matches,link_cached_identity,instant,decision
from .storage import BudgetExceeded


def record_matches(record,key):
    if not isinstance(record,dict) or demo_marked(record) or not record.get('id'):return False
    matches=addresses(record.get('formattedAddress',''))
    return len(matches)==1 and address_key(matches[0]['street'],matches[0]['city'],matches[0]['state'],matches[0]['zip'])==key


def auto_link_cached(store,now):
    for (cid,) in store.execute('SELECT id FROM realscout_candidates').fetchall():
        matches=cached_identity_matches(store,cid)
        if len(matches)==1:link_cached_identity(store,cid,matches[0],now.isoformat())


def enrich(store,boundary,settings,rs_settings,*,clock=None,client=None):
    if not rs_settings.enrichment_enabled:return dict(requests=0,processed=0,reason='selective_enrichment_disabled')
    if settings.discovery_source!='realscout' or (not settings.enabled and not getattr(client,'cache_only',False)):
        raise PermissionError('Selective enrichment requires both existing RentCast approval and RealScout discovery mode')
    clock=clock or (lambda:dt.datetime.now(dt.timezone.utc));now=clock()
    auto_link_cached(store,now)
    client=client or Client(store,settings,clock)
    processed=0;skipped=0
    rows=store.execute('SELECT id,address_key,payload,effective_at FROM realscout_candidates ORDER BY first_seen DESC,id').fetchall()
    for cid,key,raw,observed in rows:
        p=json.loads(raw)
        row=dict(id=cid,formattedAddress=p['street'],state=p['state'],zipCode=p['zipcode'],
                 price=p['price'],bedrooms=p['bedrooms'],bathrooms=p['bathrooms'],squareFootage=p['square_footage'])
        state,reasons=verification(row,boundary)
        if state=='Rejected' or any(r not in {'status','coordinates'} for r in reasons) or (not recent_timestamp(observed,now) and not getattr(client,'cache_only',False) and 'stale_email_observation' in decision(store,cid,boundary,now)['reasons']):
            skipped+=1;continue
        existing=store.execute('SELECT expires_at FROM realscout_evidence WHERE candidate_id=?',(cid,)).fetchone()
        if existing and instant(existing[0])>now:skipped+=1;continue
        if decision(store,cid,boundary,now)['state']=='Qualified':skipped+=1;continue
        link=store.execute('SELECT property_id FROM realscout_links WHERE candidate_id=?',(cid,)).fetchone()
        display=f"{p['street']}, {p['city']}, {p['state']} {p['zipcode']}"
        try:
            if link:
                cached=store.get_tax(link[0],now.isoformat())
                if cached and cached[1]==LIVE and record_matches(cached[0],key):record=cached[0];fetched=instant(cached[2])
                else:
                    record,_=client.get('/properties/'+quote(link[0],safe=''));fetched=instant(client.last_fetched_at)
            else:
                result,_=client.get('/properties',{'address':display,'limit':1});fetched=instant(client.last_fetched_at)
                record=result[0] if isinstance(result,list) and len(result)==1 else None
        except BudgetExceeded:break
        except Exception:
            # Existing Client records sanitized transport diagnostics; no email data is logged here.
            skipped+=1;continue
        if not record_matches(record,key):
            # Client's address-response/negative cache suppresses identical attempts; never guess an identity.
            skipped+=1;continue
        expiry=tax_expiry(record,fetched,settings)
        with store.transaction():
            store.execute('INSERT INTO realscout_evidence VALUES (?,?,?,?,?,?) ON CONFLICT(candidate_id) DO UPDATE SET property_id=excluded.property_id,payload=excluded.payload,source=excluded.source,fetched_at=excluded.fetched_at,expires_at=excluded.expires_at',
                          (cid,record['id'],json.dumps(record),LIVE,fetched.isoformat(),expiry.isoformat()))
            store.save_tax(record['id'],record,LIVE,fetched.isoformat(),expiry.isoformat())
        processed+=1
    return dict(requests=client.count,processed=processed,skipped=skipped)


def verify_active_status(store,boundary,settings,rs_settings,*,clock=None,client=None):
    """Optional exact-address current-status check; never a city/ZIP inventory query.
    This can cost ONE ADDITIONAL request per uncached property and is OFF by default.
    """
    if not rs_settings.status_verification_enabled:return {'requests':0,'verified':0}
    if not (rs_settings.enrichment_enabled and (settings.enabled or getattr(client,'cache_only',False)) and settings.selective_status_verification and settings.discovery_source=='realscout'):
        raise PermissionError('Selective current-status verification is not approved/enabled')
    clock=clock or (lambda:dt.datetime.now(dt.timezone.utc));now=clock()
    client=client or Client(store,settings,clock);verified=0
    for cid,key,raw in store.execute('SELECT id,address_key,payload FROM realscout_candidates ORDER BY first_seen DESC,id').fetchall():
        d=decision(store,cid,boundary,now)
        if d['state']=='Rejected' or d['state']=='Qualified':continue
        ev=store.execute('SELECT expires_at FROM realscout_status_evidence WHERE candidate_id=?',(cid,)).fetchone()
        if ev and instant(ev[0])>now:continue
        p=json.loads(raw)
        row=dict(id=cid,formattedAddress=p['street'],state=p['state'],zipCode=p['zipcode'],price=p['price'],
                 bedrooms=p['bedrooms'],bathrooms=p['bathrooms'],squareFootage=p['square_footage'])
        state,reasons=verification(row,boundary)
        if state=='Rejected' or any(r not in {'coordinates','status'} for r in reasons):continue
        display=f"{p['street']}, {p['city']}, {p['state']} {p['zipcode']}"
        try:records,_=client.get('/listings/sale',{'address':display,'limit':1,'status':'Active'})
        except BudgetExceeded:break
        except Exception:continue
        if len(records)!=1 or not record_matches(records[0],key) or records[0].get('status')!='Active':continue
        fetched=instant(client.last_fetched_at)
        store.execute('INSERT INTO realscout_status_evidence VALUES (?,?,?,?,?) ON CONFLICT(candidate_id) DO UPDATE SET payload=excluded.payload,source=excluded.source,fetched_at=excluded.fetched_at,expires_at=excluded.expires_at',
                      (cid,json.dumps(records[0]),LIVE,fetched.isoformat(),(fetched+dt.timedelta(hours=24)).isoformat()))
        verified+=1
    return {'requests':client.count,'verified':verified}
