"""Authorized RealScout email facts; normal rental criteria, no paid providers."""
import datetime as dt,hashlib,json,urllib.request,urllib.parse,urllib.error
from .collector import LIVE
from .core import verification,geographic_verification,tax_verification
from .offmarket import street_key
from .tax_backlog import review_deferred
from .request import NoRedirect
from .provenance import demo_marked


def evaluate(store,boundary,records,now,opener=None):
    if not isinstance(records,list) or len(records)>30:raise ValueError('Bounded listing intake required')
    existing=[(i,json.loads(raw)) for i,raw in store.execute('SELECT id,payload FROM properties WHERE source=?',(LIVE,)).fetchall()]
    cards=[];results=[];counts=dict(received=len(records),known_rentcast=0,not_known_rentcast=0,non_tax_pass=0,pending=0,qualified=0,rejected=0,needs_research=0,owner_deferred=0,paid_calls=0,new_unique_beyond_rentcast=0)
    geocode_budget=2
    for item in records:
        if item.get('source_id')!='realscout-email':raise ValueError('Unsupported listing source')
        p=dict(item.get('property') or {});observed=item.get('received_at');stamp=dt.datetime.fromisoformat(str(observed).replace('Z','+00:00'))
        if stamp.tzinfo is None or stamp>now+dt.timedelta(minutes=5):raise ValueError('Invalid observation time')
        for key in ('formattedAddress','addressLine1','zipCode'):
            if not isinstance(p.get(key),str) or not p[key] or len(p[key])>500:raise ValueError('Incomplete listing identity')
        matches=[(i,v) for i,v in existing if street_key(v)==street_key(p) and str(v.get('zipCode'))==p['zipCode']]
        matched=matches[0] if len(matches)==1 else None
        identity=matched[0] if matched else 'realscout:'+hashlib.sha256((street_key(p)+'|'+p['zipCode']).encode()).hexdigest()
        counts['known_rentcast' if matched else 'not_known_rentcast']+=1
        identity_key='listing_identity:'+identity
        prior_identity=store.execute('SELECT payload FROM settings WHERE name=?',(identity_key,)).fetchone()
        if not matched and not prior_identity:counts['new_unique_beyond_rentcast']+=1
        raw_evidence=dict(p)
        if matched:
            for key in ('latitude','longitude','propertyType','propertySubType','ownershipType'):
                if p.get(key) is None:p[key]=matched[1].get(key)
        p['id']=identity;p['state']='PA'
        if p.get('latitude') is None and geocode_budget>0:
            geocode_budget-=1
            location=geocode(store,p,boundary,now,opener)
            if location.get('matched'):p.update(latitude=location['latitude'],longitude=location['longitude'])
        # Source status is explicit; absent/ambiguous availability is never Active.
        p['status']=item.get('listing_status')
        state,reasons=verification(p,boundary)
        fresh=dt.timedelta(0)<=now-stamp<=dt.timedelta(hours=24)
        if not fresh:reasons.append('stale_listing_status_requires_refresh')
        if len(matches)>1:state='Pending Verification';reasons.append('ambiguous_existing_identity')
        tax=None;fetched=None
        saved=store.get_tax(identity,now.isoformat()) if matched else None
        if saved and saved[1]==LIVE and isinstance(saved[0],dict) and saved[0].get('id')==identity and not demo_marked(saved[0]):
            tax_state,value,reason=tax_verification(saved[0],now.date())
            if tax_state=='Rejected':state='Rejected';reasons.append(reason)
            elif tax_state=='Qualified':tax=value;fetched=saved[2]
        deferred=review_deferred(identity,store)
        if deferred:counts['owner_deferred']+=1
        if state=='Candidate':counts['non_tax_pass']+=1
        if state=='Rejected':counts['rejected']+=1
        elif not deferred and len(matches)<=1 and geographic_verification(p,boundary)[0]=='Candidate':
            qualified=state=='Candidate' and tax is not None and fresh
            counts['qualified' if qualified else 'pending']+=1
            cards.append(dict(source_id=identity,source='RealScout',source_label='RealScout listing alert (MLS-origin channel)',address=p['formattedAddress'],street_address=p['addressLine1'],unit=p.get('addressLine2'),city=p.get('city'),state='PA',zip_code=p['zipCode'],latitude=p.get('latitude'),longitude=p.get('longitude'),price=p.get('price'),bedrooms=p.get('bedrooms'),bathrooms=p.get('bathrooms'),square_feet=p.get('squareFootage'),annual_tax=tax['annual'] if qualified else None,tax_year=tax['year'] if qualified else None,tax_status='provider_verified' if qualified else 'pending_verification',tax_source='RentCast cached Property Records' if qualified else None,qualification='verified' if qualified else 'pending_verification',section='new' if qualified else 'pending_verification',reasons=reasons+([] if qualified else ['tax_verification_required']),discovered_at=observed,updated_at=observed,listing_observed_at=observed,verified_at=now.isoformat() if qualified else None,tax_fetched_at=fetched if qualified else None,listing_url=item.get('evidence_url'),last_reported_status=p['status']))
        elif state!='Rejected' and not deferred:counts['needs_research']+=1
        result=dict(source_id=identity,property=p,source_property=raw_evidence,source='realscout-email',observed_at=observed,state=state,reasons=reasons,matched_rentcast=bool(matched),owner_deferred=deferred)
        key='listing_observation:'+hashlib.sha256((str(item.get('external_id'))+'|'+identity).encode()).hexdigest()
        store.save_setting(key,result)
        if not prior_identity or json.loads(prior_identity[0])['observed_at']<=observed:store.save_setting(identity_key,result)
        results.append(result)
    return dict(cards=cards,counts=counts,evaluations=results)


def geocode(store,p,boundary,now,opener=None):
    """Bounded Census public address geocoder; never a parcel or tax verification."""
    key='listing_geocode:'+hashlib.sha256((street_key(p)+'|'+p['zipCode']).encode()).hexdigest()
    row=store.execute('SELECT payload FROM settings WHERE name=?',(key,)).fetchone()
    if row:
        cached=json.loads(row[0])
        if cached.get('next_at','')>now.isoformat():return cached
    if store.execute('SELECT 1 FROM settings WHERE name=?',('listing_geocoder_access_denied',)).fetchone():return {'matched':False,'reason':'official_geocoder_access_denied'}
    result={'matched':False,'source':'US Census public address geocoder','retrieved_at':now.isoformat(),'next_at':(now+dt.timedelta(days=7)).isoformat()}
    url='https://geocoding.geo.census.gov/geocoder/locations/onelineaddress?'+urllib.parse.urlencode({'address':p['formattedAddress'],'benchmark':'Public_AR_Current','format':'json'})
    try:
        opener=opener or urllib.request.build_opener(NoRedirect)
        with opener.open(urllib.request.Request(url,headers={'User-Agent':'RentalSourcing authorized listing intake','Accept':'application/json'}),timeout=12) as response:
            raw=response.read(100001)
            if len(raw)>100000:raise ValueError('Oversized geocoder response')
        matches=json.loads(raw)['result']['addressMatches']
        if len(matches)==1:
            m=matches[0];components=m['addressComponents'];coords=m['coordinates']
            exact=street_key({'formattedAddress':m['matchedAddress']})==street_key(p) and str(components.get('zip'))==p['zipCode'] and components.get('state')=='PA'
            if exact and not p.get('addressLine2') and not any(x in street_key(p).split() for x in ('unit','apt')):
                x,y=float(coords['x']),float(coords['y']);locations=[{**p,'longitude':x+dx,'latitude':y+dy} for dx,dy in [(0,0),(.0002,0),(-.0002,0),(0,.0002),(0,-.0002)]]
                if all(geographic_verification(v,boundary)[0]=='Candidate' for v in locations):result.update(matched=True,longitude=x,latitude=y,precision='official_street_interpolation_not_parcel',source_url=url)
    except urllib.error.HTTPError as exc:
        result['reason']='official_geocoder_http_'+str(exc.code)
        if exc.code in (401,403):store.save_setting('listing_geocoder_access_denied',{'at':now.isoformat(),'status':exc.code})
        else:result['next_at']=(now+dt.timedelta(minutes=30)).isoformat()
    except Exception as exc:
        result.update(reason='official_geocoder_unavailable',next_at=(now+dt.timedelta(minutes=30)).isoformat())
    store.save_setting(key,result);return result


def summary(store):
    records=[json.loads(raw) for raw, in store.execute('SELECT payload FROM settings WHERE name LIKE ?',('listing_identity:%',)).fetchall()]
    return {'unique_properties':len(records),'known_rentcast_unique':sum(r['matched_rentcast'] for r in records),'unique_beyond_rentcast':sum(not r['matched_rentcast'] for r in records),'owner_deferred_unique':sum(r['owner_deferred'] for r in records),'rejected_unique':sum(r['state']=='Rejected' for r in records),'non_tax_pass_unique':sum(r['state']=='Candidate' for r in records),'source_observations':store.execute('SELECT COUNT(*) FROM settings WHERE name LIKE ?',('listing_observation:%',)).fetchone()[0],'paid_provider_calls':0}
