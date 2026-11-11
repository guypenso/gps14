"""Property-specific structured type evidence; broad labels cannot erase subtypes."""
import json,re
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent/'data'
POLICY=json.loads((ROOT/'property-type-policy.json').read_text())
REVIEWED=json.loads((ROOT/'reviewed-property-types.json').read_text())
def compact(v):return re.sub(r'[^a-z0-9]','',str(v).lower())
def address(v):
    words={'street':'st','avenue':'ave','road':'rd','drive':'dr','north':'n','south':'s','east':'e','west':'w'}
    return ' '.join(words.get(w,w) for w in re.sub(r'[^a-z0-9 ]',' ',str(v).lower()).split())
def reviewed_type(p):
    if p.get('addressLine2') or p.get('unit'):return None
    a=address(p.get('addressLine1') or p.get('street_address') or str(p.get('formattedAddress') or p.get('address') or '').split(',')[0]);z=str(p.get('zipCode') or p.get('zip_code') or '').split('-')[0]
    return next((r for r in REVIEWED if address(r['street_address'])==a and r['zip_code']==z and p.get('state','PA')=='PA'),None)
def type_fields(row):
    found=[]
    def walk(p,path='',depth=0):
        if not isinstance(p,dict) or depth>3:return
        allowed={compact(k) for k in POLICY['fields']};containers={compact(k) for k in POLICY['containers']}
        for field,v in p.items():
            if compact(field) in allowed:
                for value in v if isinstance(v,list) else [v]:
                    if isinstance(value,(str,bool)) and value!='':found.append({'field':path+field,'value':value})
            if compact(field) in containers and isinstance(v,dict):walk(v,path+field+'.',depth+1)
    walk(row)
    return found
def property_type_evidence(row,*,single_family=True):
    fields=type_fields(row);review=reviewed_type(row)
    if review:fields.append({'field':'reviewed.propertySubType','value':review['propertySubType']})
    prior=row.get('_rentalsourcing_prior_property_type')
    if isinstance(prior,dict) and prior.get('state')=='excluded':fields+=prior.get('evidence',[])[:20]
    values=[compact(f['value']) for f in fields if compact(f['field'].split('.')[-1])!='architecturalstyle' and isinstance(f['value'],str) and not re.search(r'co\W*op|cooperative|unknown|uncertain',f['value'],re.I) and not re.search(r'\b(?:not|non|no)\W+(?:a\W+)?condo(?:minium)?\b',f['value'],re.I)]
    def contains(terms):return any(term in value for value in values for term in terms)
    reason='condominium' if contains(POLICY['condo_terms']) or any(compact(f['field'].split('.')[-1]) in ('iscondominium','iscondo') and f['value'] is True for f in fields) else 'townhouse' if contains(POLICY['town_terms']) else None
    if reason:state='excluded'
    elif any(v in ('land','commercial','industrial') for v in values):state,reason='excluded','non_residential'
    elif single_family and any(v in ('duplex','triplex','fourplex','multifamily','manufactured','manufacturedhome','mobilehome','residentialincome') for v in values):state,reason='excluded','single_family_only'
    elif any(compact(f['field'].split('.')[-1])=='architecturalstyle' and any(term in compact(f['value']) for term in POLICY['town_terms']) for f in fields):state,reason='pending','architectural_attachment_requires_verification'
    elif review and review.get('type_conflict'):state,reason='pending','conflicting_property_type'
    elif any(compact(f['field'].split('.')[-1]) in ('attached','isattached') and f['value'] is True for f in fields) or any(v in ('attached','semidetached','singlefamilysemidetached','semidetachedsinglefamily','duet','twinhome') for v in values):state,reason='pending','attached_property_type_unverified'
    elif any(v in POLICY['single_family_terms'] or not single_family and v in ('multifamily','duplex','triplex','fourplex','manufactured','manufacturedhome','mobilehome','residentialincome') for v in values):state,reason='eligible',None
    else:state,reason='pending','property_type_unverified'
    detail=state=='eligible' and any('subtype' in compact(f['field']) and compact(f['value']) in POLICY['single_family_terms'] or compact(f['value']) in ('detached','singlefamilydetached','detachedsinglefamily') or compact(f['field'].split('.')[-1]) in ('attached','isattached') and f['value'] is False for f in fields)
    return {'policy':POLICY['version'],'state':state,'reason':reason,'detail_confirmed':bool(detail),'evidence':fields[:40],'reviewed_source':review}
