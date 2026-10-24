"""Reuse existing RealScout verified inbox, RentCast Client/cache and county connector."""
from dataclasses import replace
import datetime as dt
import json
from .models import Listing,Source,normalize,instant
from .repository import register,ingest,health,resolution
from .evaluate import evaluate
from ..core import tax_verification,verification
from ..realscout import addresses,address_key
from ..provenance import demo_marked
from ..storage import BudgetExceeded


def exact_record(record,key):
    if not isinstance(record,dict) or not record.get('id') or demo_marked(record):return False
    found=addresses(record.get('formattedAddress',''))
    return len(found)==1 and address_key(found[0]['street'],found[0]['city'],found[0]['state'],found[0]['zip'])==key


def sync_realscout(store,now,source,*,public_app_url=''):
    """Read only already-ingested authorized cards; never reads mailbox or opens a URL."""
    if source.kind!='realscout':raise ValueError('Wrong source type')
    count=0
    for cid,payload,observed in store.execute('SELECT id,payload,effective_at FROM realscout_candidates').fetchall():
        p=json.loads(payload);fields={k:p.get(v) for k,v in {'price':'price','bedrooms':'bedrooms','bathrooms':'bathrooms','squareFootage':'square_footage'}.items()}
        fields.update(state=p['state'],zipCode=p['zipcode'])
        if p.get('alert_profile')=='saved_search' and p.get('listing_status'):fields['status']=p['listing_status']
        # Only cryptographically ingested messages or explicit sample-mode imports are eligible.
        auth=store.execute('SELECT 1 FROM realscout_observations o JOIN realscout_auth_evidence a ON a.message_key=o.message_id WHERE o.candidate_id=?',(cid,)).fetchone()
        if not auth and not source.synthetic:continue
        link=None
        item=Listing(source,cid,p['street'],p['city'],p['state'],p['zipcode'],observed,fields,p.get('listing_id'),None,link)
        if public_app_url:item=replace(item,url=public_app_url.rstrip('/')+'/discovery/detail/'+item.entity_id)
        result=ingest(store,item,now,authorized=True);count+=not result['duplicate']
    register(store,source)
    # No scan-success clock: this is an existing inbox bridge, not a new MLS scan.
    health(store,source.name,now,error=None)
    return dict(source=source.name,records=count,mode='ingested_alert_bridge',fresh_scan=False)


class RentCastAdapter:
    def __init__(self,source,client,*,discovery_enabled=False,rent_enabled=False,status_enabled=False):
        if source.kind!='rentcast' or not source.enabled:raise PermissionError('Authorized RentCast source required')
        self.source=source;self.client=client;self.discovery_enabled=discovery_enabled
        self.rent_enabled=rent_enabled;self.status_enabled=status_enabled
    def scan(self,store,now):
        register(store,self.source)
        if not self.discovery_enabled:return dict(source=self.source.name,mode='discovery_disabled',fresh_scan=False,records=0)
        before=self.client.count
        try:
            rows,total=self.client.get('/listings/sale',{'city':'Pittsburgh','state':'PA','status':'Active','limit':500,'offset':0},purpose='discovery')
            stamp=self.client.last_fetched_at
            for row in rows:ingest(store,normalize(row,self.source,observed_at=stamp,style='rentcast'),now,authorized=True)
            wire=self.client.count-before;complete=bool(total is not None and int(total)<=len(rows))
            health(store,self.source.name,now,success=wire>0,error=None if complete else 'coverage_page_unverified')
            return dict(source=self.source.name,records=len(rows),requests=wire,fresh_scan=wire>0,mode='provider_query_page' if wire else 'cached_provider_page',all_pages_consumed=complete)
        except Exception as exc:
            health(store,self.source.name,now,error=type(exc).__name__)
            return dict(source=self.source.name,records=0,requests=self.client.count-before,fresh_scan=False,error=type(exc).__name__)
    def enrich(self,store,boundary,now,max_candidates=3):
        register(store,self.source);before=self.client.count;processed=0;errors=[]
        rows=store.execute('SELECT id,address_key,address FROM discovery_entities ORDER BY first_seen DESC,id').fetchall()
        def priority(row):
            eid=row[0];fields=resolution(store,eid,now)['fields']
            reduction=store.execute("SELECT 1 FROM discovery_observations WHERE entity_id=? AND kind='price_reduction' LIMIT 1",(eid,)).fetchone()
            return (not bool(reduction),not any(p.get('source_kind')=='mls' for p in resolution(store,eid,now)['provenance'].values()),
                fields.get('price',float('inf'))/fields.get('squareFootage',1) if fields.get('squareFootage') else float('inf'),eid)
        rows.sort(key=priority)
        for eid,key,address in rows:
            if processed>=max_candidates:break
            state=evaluate(store,eid,boundary,now)
            if state['status']=='Rejected':continue
            fields=state['fields'];processed+=1
            try:
                if fields.get('annual_tax') is None or fields.get('latitude') is None or fields.get('longitude') is None:
                    records,_=self.client.get('/properties',{'address':address,'limit':1},purpose='enrichment')
                    record=records[0] if isinstance(records,list) and len(records)==1 else None
                    if not exact_record(record,key):errors.append('property_identity_unverified');continue
                    item=normalize(record,self.source,observed_at=self.client.last_fetched_at,style='rentcast')
                    actual=dict(item.fields);_,tax,_=tax_verification(record,now.date())
                    if tax:actual.update(annual_tax=tax['annual'],tax_year=tax['year'])
                    ingest(store,replace(item,record_id='property:'+item.record_id,fields=actual),now,authorized=True)
                fields=resolution(store,eid,now)['fields']
                if self.status_enabled and fields.get('status') is None:
                    records,_=self.client.get('/listings/sale',{'address':address,'limit':1,'status':'Active'},purpose='verification')
                    if len(records)==1 and exact_record(records[0],key):
                        ingest(store,normalize(records[0],self.source,observed_at=self.client.last_fetched_at,style='rentcast'),now,authorized=True)
                state=evaluate(store,eid,boundary,now);fields=state['fields']
                basic=verification(dict(fields,id=eid,formattedAddress=address),boundary)[0]
                single=fields.get('propertyType') in {'Single Family','SingleFamilyResidence','SingleFamily','Condominium','Condo','Townhouse','Manufactured','ManufacturedHome'}
                if self.rent_enabled and state['status']!='Rejected' and basic=='Candidate' and single and fields.get('monthly_rent') is None:
                    estimate,_=self.client.get('/avm/rent/long-term',{'address':address},purpose='rent_estimate')
                    subject=estimate.get('subjectProperty',{})
                    if not exact_record(subject,key) or not isinstance(estimate.get('rent'),(int,float)):errors.append('rent_estimate_identity_or_scope_unverified');continue
                    item=normalize(subject,self.source,observed_at=self.client.last_fetched_at,style='rentcast')
                    actual={'monthly_rent':estimate['rent']}
                    for old,new in [('rentRangeLow','rent_low'),('rentRangeHigh','rent_high')]:
                        if estimate.get(old) is not None:actual[new]=estimate[old]
                    comps=tuple({k:r[k] for k in ('id','formattedAddress','price','distance','correlation','lastSeenDate') if k in r} for r in estimate.get('comparables',[])[:25] if isinstance(r,dict))
                    ingest(store,replace(item,record_id='rent:'+item.record_id,fields=actual,estimates=tuple(actual),comparables=comps),now,authorized=True)
            except BudgetExceeded:errors.append('approved_budget_unavailable');break
            except Exception as exc:errors.append(type(exc).__name__)
        return dict(processed=processed,requests=self.client.count-before,errors=sorted(set(errors)))


def sync_legacy_rentcast(store,source,now):
    """Import existing verified cache/history; never reset provenance, counters or timestamps."""
    count=0
    for identity,raw,observed,origin in store.execute('SELECT id,payload,observed_at,source FROM properties').fetchall():
        if origin!='rentcast-live':continue
        try:
            item=normalize(json.loads(raw),source,observed_at=observed,style='rentcast')
            result=ingest(store,item,now,authorized=True);count+=not result['duplicate']
            tax=store.get_tax(identity,now.isoformat())
            if tax and tax[1]=='rentcast-live' and tax[0].get('id')==identity and not demo_marked(tax[0]):
                _,amount,_=tax_verification(tax[0],now.date())
                if amount:ingest(store,replace(item,record_id='property:'+identity,observed_at=tax[2],fields={'annual_tax':amount['annual'],'tax_year':amount['year']}),now,authorized=True)
        except (ValueError,PermissionError):continue
    return count
