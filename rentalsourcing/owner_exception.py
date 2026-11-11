"""Read-only stored evidence for the owner's single expressly authorized address."""
import json,re
from .public_tax import norm
from .provenance import demo_marked
ADDRESS='4472 1st St'
ZIP='15102'
KEY='owner-exception-4472-1st-st-20261112'
def matches(p):
    street=p.get('addressLine1') or p.get('street_address') or str(p.get('formattedAddress') or p.get('address') or '').split(',')[0]
    normalized=norm(re.sub(r'\bfirst\b','1st',str(street),flags=re.I))
    return normalized==norm(ADDRESS) and str(p.get('zipCode') or p.get('zip_code') or '')[:5]==ZIP and p.get('state','PA')=='PA' and not (p.get('unit') or p.get('addressLine2'))
def snapshot(store,now):
    from .core import verification
    from .geography import load_boundary
    from .config import Settings
    from .rental_intelligence import cached_estimate
    rows=store.execute('SELECT id,payload,state,reasons,observed_at FROM properties').fetchall()
    found=[];boundary=load_boundary(Settings.from_env().boundary_file)
    for identity,raw,state,reasons,observed in rows:
        p=json.loads(raw)
        if not matches(p):continue
        fields=('id','formattedAddress','addressLine1','city','state','zipCode','propertyType','propertySubType','bedrooms','bathrooms','squareFootage','price','status','latitude','longitude','parcelId')
        verdict,failures=verification(p,boundary)
        cached_tax=None;saved=store.get_tax(identity,now.isoformat())
        if saved and saved[1]==__import__('rentalsourcing.collector',fromlist=['LIVE']).LIVE:
            from .core import tax_verification
            record=json.loads(saved[0]) if isinstance(saved[0],str) else saved[0]
            if isinstance(record,dict) and record.get('id')==identity and matches(record) and not demo_marked(record):
                tax_state,tax,_=tax_verification(record,now.date())
                if tax_state in ('Qualified','Rejected') and tax:cached_tax={'annual':tax['annual'],'year':tax['year'],'fetched_at':saved[2],'source':'RentCast cached property records'}
        found.append({'cached_tax':cached_tax,'source_id':identity,'property':{k:p[k] for k in fields if k in p},'stored_state':state,'stored_reasons':json.loads(reasons),'current_state':verdict,'current_reasons':failures,'observed_at':observed,'cached_rent':cached_estimate(store,p,now)})
    observations=[]
    for raw, in store.execute('SELECT payload FROM settings WHERE name LIKE ?',('listing_observation:%',)).fetchall():
        item=json.loads(raw);p=item.get('property',{})
        if matches(p):observations.append({k:item.get(k) for k in ('source_id','listing_id','source','observed_at','state','reasons','listing_url')})
    return {'approval_key':KEY,'authorized_address':'4472 1st St, Bethel Park, PA 15102','source_records_checked':len(rows),'matches':found,'listing_observations':observations,'checked_at':now.isoformat(),'provider_calls':0}
