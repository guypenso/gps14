"""Evidence-bounded acquisition policy. No external requests, personal distress inference or invented facts."""
import datetime as dt,re
from urllib.parse import urlsplit
from .offmarket import street_key
UTC=dt.timezone.utc
VERSION='offmarket-acquisition-v2'
REQUIRED=('zillow','redfin','realtor')
PLATFORMS={'zillow':'zillow.com','redfin':'redfin.com','realtor':'realtor.com','homes':'homes.com','trulia':'trulia.com'}

def stamp(value):
 try:
  d=dt.datetime.fromisoformat(str(value).replace('Z','+00:00'))
  return d if d.tzinfo else None
 except (ValueError,TypeError):return None

def fresh(e,now,days):
 date=stamp(e.get('observed_at'));expiry=stamp(e.get('expires_at'))
 return bool(date and dt.timedelta(minutes=-5)<=now-date<=dt.timedelta(days=days) and (not expiry or expiry>=now))

def domain(url,expected):
 try:
  u=urlsplit(url);return u.scheme=='https' and not u.username and not u.password and (u.hostname==expected or u.hostname.endswith('.'+expected))
 except (ValueError,AttributeError,TypeError):return False

def identity(e,p):
 # Backend-only evidence contains the exact property identity reviewed by an admin or official matcher.
 q=e.get('matched_property',{})
 if q.get('parcel_id') and p.get('parcel_id') and q['parcel_id']!=p['parcel_id']:return False
 return bool(street_key(q) and street_key(q)==street_key(p) and q.get('zipCode') and q.get('zipCode')==p.get('zipCode'))

def market(p,evidence,cached,now):
 checked=[];public=False;delisted=False
 if cached:
  row,observed=cached;fetched=stamp(observed)
  current=bool(fetched and dt.timedelta(0)<=now-fetched<=dt.timedelta(days=1))
  status=str(row.get('status','')).lower().replace(' ','')
  public=current and status in ('active','comingsoon')
  delisted=current and status in ('inactive','withdrawn','expired','cancelled','offmarket')
  checked.append({'source':'existing_rentcast','status':row.get('status'),'checked_at':now.isoformat(),'observed_at':observed,'fresh':current,'method':'stored_observation_only','source_url':'https://www.rentcast.io/','note':'No provider request. Absence from stored inventory does not prove off-market.'})
 for name,host in PLATFORMS.items():
  found=[e for e in evidence if e.get('kind')=='market' and e.get('source_name')==name and domain(e.get('source_url'),host) and identity(e,p) and fresh(e,now,1)]
  found.sort(key=lambda e:e['observed_at'],reverse=True)
  if found:
   newest=found[0];same=[e for e in found if e['observed_at']==newest['observed_at']];states={e.get('status') for e in same}
   status=newest.get('status') if len(states)==1 else 'conflicting'
   public|=status in ('active','coming_soon');delisted|=status=='delisted'
   checked.append({'source':name,'status':status,'checked_at':newest.get('recorded_at'),'observed_at':newest['observed_at'],'source_url':newest['source_url'],'method':newest.get('verification_method'),'fresh':True})
  else:checked.append({'source':name,'status':'not_checked','fresh':False,'dependency':'Authorized current listing feed or explicit administrator verification of this exact property. No protected-site scraping.'})
 negative={r['source'] for r in checked if r['status'] in ('no_active_sale','delisted') and r.get('fresh')}
 state='Publicly Listed' if public else 'Recently Delisted' if delisted else 'Verified Off-Market' if set(REQUIRED)<=negative else 'Verification Incomplete'
 # Recent delisting is a separate research finding; full negative coverage is still necessary for readiness.
 return {'state':state,'bounded_verified':not public and set(REQUIRED)<=negative,'required_sources':list(REQUIRED),'sources_checked':checked,'checked_at':now.isoformat(),'coverage':'Only explicitly identified, successfully checked sources; never a guarantee of absence everywhere.','recently_delisted':delisted}

def condition(p,evidence,now):
 found=[e for e in evidence if e.get('kind')=='condition' and identity(e,p) and fresh(e,now,180)]
 severe=[e for e in found if e.get('category')=='Major Rehabilitation' and e.get('status') in ('active','confirmed')]
 complete=[e for e in found if e.get('category') in ('Light Renovation','Moderate Renovation') and e.get('scope')=='whole_property' and e.get('status')=='confirmed']
 complete.sort(key=lambda e:e['observed_at'],reverse=True)
 if severe:return {'category':'Major Rehabilitation','excluded':True,'inspection_required':True,'evidence':severe,'reason':'documented_severe_rehabilitation'}
 if complete:return {'category':complete[0]['category'],'excluded':False,'inspection_required':True,'evidence':[complete[0]],'reason':None}
 return {'category':'Condition Unknown','excluded':False,'inspection_required':True,'evidence':found,'reason':'current_whole_property_condition_evidence_required'}

def price(p,item,evidence,now):
 found=[e for e in evidence if e.get('kind')=='asking_price' and e.get('price_kind')=='asking' and e.get('verification_method') in ('administrator_review','authorized_supplier','official_api','official_inventory') and identity(e,p) and fresh(e,now,7) and domain(e.get('source_url'),urlsplit(e.get('source_url','')).hostname or '!') and isinstance(e.get('amount'),(int,float)) and not isinstance(e.get('amount'),bool) and e['amount']==p.get('price')]
 found.sort(key=lambda e:e['observed_at'],reverse=True)
 e=found[0] if found else None
 return {'state':'verified' if e else 'unknown','amount':e['amount'] if e else None,'in_range':bool(e and 30000<=e['amount']<=200001),'source_url':e.get('source_url') if e else None,'verified_at':e.get('observed_at') if e else None,'reason':None if e and 30000<=e['amount']<=200001 else 'asking_price_out_of_range' if e else 'documented_asking_price_required'}

def contact(p,item,evidence,now):
 routes=[]
 for e in evidence:
  program=e.get('program_inventory_verified') is True and e.get('role')=='Public Agency' and e.get('verification_method')=='official_inventory' and e.get('source_url')=='https://pghlandbank.org/pilot-residential-rehab-for-sale/' and item.get('source_id')=='pittsburgh-land-bank' and e.get('offering_street_key')==street_key(p)
  if e.get('kind')!='contact' or not (identity(e,p) or program) or not fresh(e,now,90):continue
  role=e.get('role');email=e.get('email');phone=e.get('phone');url=e.get('contact_url');source=e.get('source_url')
  if role not in ('Public Agency','Wholesaler','Agent','Broker','Owner','Property Manager') or not source or not domain(source,urlsplit(source).hostname or '!'):continue
  if not e.get('name'):continue
  if url and not domain(url,urlsplit(url).hostname or '!'):continue
  if not (email and re.fullmatch(r'[^\s@\r\n]+@[^\s@\r\n]+\.[^\s@\r\n]+',email) or phone and len(re.sub(r'\D','',phone))>=10 or url):continue
  status='Official Contact Route Available' if role=='Public Agency' and e.get('relationship_confirmed') and e.get('verification_method') in ('administrator_review','official_inventory','official_api') else 'Verified Contact Channel' if e.get('verification_method') in ('administrator_review','authorized_supplier','official_api') and e.get('relationship_confirmed') else 'Likely Professional Contact - Review Required'
  routes.append({k:e.get(k) for k in ('name','role','email','phone','contact_url','source_url','observed_at','verification_method','relationship','relationship_confirmed')}|{'status':status,'usable':status in ('Official Contact Route Available','Verified Contact Channel')})
 if not routes and item.get('contact_email'):
  routes.append({'name':item.get('contact_name'),'role':item.get('contact_role','Wholesaler'),'email':item['contact_email'],'source_url':item.get('evidence_url'),'status':'Likely Professional Contact - Review Required','usable':False})
 return {'state':next((r['status'] for r in routes if r.get('usable')),routes[0]['status'] if routes else 'Contact Research Required'),'usable':any(r.get('usable') for r in routes),'routes':routes}

def analyze(p,item,base_state,base_reasons,tax,evidence,cached,now):
 m=market(p,evidence,cached,now);c=condition(p,evidence,now);r=contact(p,item,evidence,now);asking=price(p,item,evidence,now)
 signals=[e for e in evidence if e.get('kind')=='official_signal' and identity(e,p)]
 current_signals=[e for e in signals if fresh(e,now,180)]
 reasons=list(base_reasons);offering=item.get('kind')=='offering'
 physical=base_state=='Candidate';potential=not offering and physical and bool(current_signals)
 hard='Rejected' if base_state=='Rejected' or item.get('purchase_eligibility')=='ineligible' else 'Pending Verification' if not physical or tax.get('state')!='verified' else 'Qualified'
 if item.get('purchase_eligibility')=='ineligible':reasons.append('acquisition_restricted')
 if m['state']=='Publicly Listed':reasons.append('confirmed_active_public_listing')
 if c['excluded']:reasons.append('documented_severe_rehabilitation')
 rejected=asking['state']=='verified' and not asking['in_range'] or hard=='Rejected' or m['state']=='Publicly Listed' or c['excluded']
 if asking['reason']:reasons.append(asking['reason'])
 if tax.get('state')!='verified':reasons.append('annual_tax_bill_unverified')
 if not m['bounded_verified']:reasons.append('listing_verification_incomplete')
 if c['category']=='Condition Unknown':reasons.append('condition_verification_required')
 if not r['usable']:reasons.append('contact_research_required')
 if offering and item.get('purchase_eligibility')!='permitted':reasons.append('investor_rental_use_requires_confirmation')
 available=offering and item.get('availability')=='available' and bool(item.get('evidence_url')) and fresh({'observed_at':item.get('received_at',now.isoformat())},now,7)
 ready=asking['in_range'] and available and hard=='Qualified' and m['bounded_verified'] and not rejected and c['category'] in ('Light Renovation','Moderate Renovation') and r['usable'] and item.get('purchase_eligibility')=='permitted'
 queue='Rejected' if rejected else 'Ready to Pursue' if ready else 'Research Required' if not r['usable'] or not signals and not offering else 'Potential Seller' if potential else 'Pending Verification'
 state='Rejected' if rejected else 'Qualified' if ready else 'Pending Verification'
 points=[]
 if not rejected:
  if physical:points.append(('verified_known_physical_geographic_criteria',30))
  if tax.get('state')=='verified':points.append(('verified_tax_gate',15))
  if m['bounded_verified']:points.append(('bounded_current_off_market_evidence',20))
  if c['category'] in ('Light Renovation','Moderate Renovation'):points.append(('supported_manageable_renovation',15))
  if r['usable']:points.append(('usable_sourced_contact_route',10))
  if available:points.append(('confirmed_offering_evidence',10))
 next_action='Review exclusion evidence; retain history' if rejected else 'Verify latest annual tax bill' if tax.get('state')!='verified' else 'Complete authorized listing-status checks' if not m['bounded_verified'] else 'Obtain current inspection or condition evidence' if c['category']=='Condition Unknown' else 'Research a property-associated professional contact route' if not r['usable'] else 'Confirm investor/rental purchase requirements' if offering and item.get('purchase_eligibility')!='permitted' else 'Request availability information; sale is not assumed' if not offering else 'Review evidence and prepare an offer'
 return {'version':VERSION,'queue':queue,'state':state,'hard_criteria_state':hard,'potential_seller':potential,'potential_label':'Potential Seller - Not Confirmed for Sale' if not offering else None,'market':m,'condition':c,'contact':r,'official_signals':signals,'reasons':list(dict.fromkeys(reasons)),'score':sum(v for _,v in points),'score_explanation':[{'factor':k,'points':v} for k,v in points],'next_action':('Obtain a documented current asking price' if asking['state']!='verified' else next_action),'asking_price':asking,'evidence':evidence}
