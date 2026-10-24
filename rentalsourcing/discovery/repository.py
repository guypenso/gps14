"""Durable normalized observations; fresh authoritative evidence wins, conflicts remain visible."""
import datetime as dt
import hashlib,json
from .models import Listing,Source,instant,FIELDS


def register(store,source):
    payload=json.dumps(source.__dict__,sort_keys=True)
    old=store.execute('SELECT payload FROM discovery_sources WHERE name=?',(source.name,)).fetchone()
    if old:
        before=json.loads(old[0])
        if any(before[k]!=getattr(source,k) for k in ('kind','authoritative','synthetic','scope')):
            raise ValueError('Source trust contract changed; register a distinct source')
    store.execute('INSERT INTO discovery_sources(name,payload) VALUES (?,?) ON CONFLICT(name) DO UPDATE SET payload=excluded.payload',(source.name,payload))


def ingest(store,listing,now,*,authorized=False):
    if not authorized:raise PermissionError('Authorized source processing required')
    if instant(listing.observed_at)>now:raise ValueError('Future source observations are not accepted')
    if listing.deleted:
        from dataclasses import replace
        listing=replace(listing,fields={**listing.fields,'status':'Off Market'})
    payload=listing.payload();encoded=json.dumps(payload,sort_keys=True)
    oid=hashlib.sha256(encoded.encode()).hexdigest()
    with store.transaction():
        if store.pg:store.execute('SELECT pg_advisory_xact_lock(73541209)')
        register(store,listing.source)
        store.execute('INSERT INTO discovery_source_metrics VALUES (?,1,0) ON CONFLICT(source) DO UPDATE SET attempts=discovery_source_metrics.attempts+1',(listing.source.name,))
        if store.execute('SELECT 1 FROM discovery_observations WHERE id=?',(oid,)).fetchone():
            store.execute('UPDATE discovery_source_metrics SET duplicates=duplicates+1 WHERE source=?',(listing.source.name,))
            return {'duplicate':True,'entity_id':listing.entity_id,'kind':'duplicate'}
        old=store.execute('SELECT entity_id,payload,observed_at FROM discovery_records WHERE source=? AND record_id=?',(listing.source.name,listing.record_id)).fetchone()
        if old and old[0]!=listing.entity_id:raise ValueError('Source identity moved to another address/unit; manual reconciliation required')
        if old and instant(old[2])==instant(listing.observed_at) and json.loads(old[1])!=payload:
            raise ValueError('Conflicting same-time observations')
        entity=store.execute('SELECT version FROM discovery_entities WHERE id=?',(listing.entity_id,)).fetchone()
        kind='new' if not entity else 'additional_source' if not old else 'refresh'
        meaningful=False
        if old:
            previous=json.loads(old[1]);oldprice=previous['fields'].get('price');price=listing.fields.get('price')
            if instant(listing.observed_at)<instant(old[2]) or (listing.modified_at and previous.get('modified_at') and instant(listing.modified_at)<instant(previous['modified_at'])):kind='historical'
            elif price is not None and oldprice is not None and price!=oldprice:kind='price_reduction' if price<oldprice else 'price_increase'
            elif previous['fields']!=listing.fields or previous.get('deleted')!=listing.deleted:kind='facts_changed'
        meaningful=kind in {'new','additional_source','price_reduction','price_increase','facts_changed'}
        if not entity:
            address=f'{listing.street}, {listing.city}, {listing.state} {listing.zipcode}'
            store.execute('INSERT INTO discovery_entities VALUES (?,?,?,?,?,1)',(listing.entity_id,listing.key,address,now.isoformat(),now.isoformat()))
        elif meaningful:store.execute('UPDATE discovery_entities SET version=version+1,last_changed=? WHERE id=?',(now.isoformat(),listing.entity_id))
        store.execute('INSERT INTO discovery_observations VALUES (?,?,?,?,?,?,?,?)',
            (oid,listing.source.name,listing.record_id,listing.entity_id,encoded,listing.observed_at,now.isoformat(),kind))
        if kind!='historical':
            store.execute('INSERT INTO discovery_records VALUES (?,?,?,?,?) ON CONFLICT(source,record_id) DO UPDATE SET payload=excluded.payload,observed_at=excluded.observed_at',
              (listing.source.name,listing.record_id,listing.entity_id,encoded,listing.observed_at))
    return {'duplicate':False,'entity_id':listing.entity_id,'kind':kind}


def resolution(store,entity_id,now):
    rows=store.execute('SELECT payload FROM discovery_records WHERE entity_id=?',(entity_id,)).fetchall()
    evidence={k:[] for k in FIELDS};stale=[]
    for (raw,) in rows:
        p=json.loads(raw);source=p['source'];observed=instant(p['observed_at']);age=(now-observed).total_seconds()
        for field,value in p['fields'].items():
            if value is None:continue
            hours=24 if field in {'price','status','days_on_market'} else 24*7 if field in {'annual_tax','tax_year','monthly_rent','estimated_value','rent_low','rent_high'} else 24*31
            if not 0<=age<=hours*3600:
                stale.append({'field':field,'source':source['name'],'observed_at':p['observed_at']});continue
            priority=100 if source['kind']=='mls' and source['authoritative'] else 90 if source['kind']=='public_records' and source['authoritative'] else 70 if source['kind']=='rentcast' else 50
            evidence[field].append(dict(value=value,source=source['name'],kind='third_party_estimate' if field in p['estimates'] else 'verified_source',
                observed_at=p['observed_at'],priority=priority,source_kind=source['kind'],synthetic=source['synthetic'],record_id=p['record_id']))
    selected={};conflicts={};provenance={}
    for field,items in evidence.items():
        if not items:continue
        items.sort(key=lambda x:(x['priority'],instant(x['observed_at'])),reverse=True);winner=items[0]
        # Compare newest observation per source so a stale relisting from that source cannot vote twice.
        votes={}
        for item in items:
            if item['source'] not in votes or instant(item['observed_at'])>instant(votes[item['source']]['observed_at']):votes[item['source']]=item
        differing=[v for v in votes.values() if v['value']!=winner['value']]
        # A newer authoritative listing supersedes an older lower-trust value; any same/higher trust
        # or newer conflicting evidence remains explicit and blocks qualification for critical fields.
        material=[v for v in differing if v['priority']>=winner['priority'] or instant(v['observed_at'])>=instant(winner['observed_at'])]
        if material:conflicts[field]=[winner]+material
        selected[field]=winner['value'];provenance[field]=winner
    # Annual bill and year are an atomic pair, and the latest reported year wins.
    # Never combine an older MLS amount with a newer aggregator year.
    tax_rows=[]
    for (raw,) in rows:
        p=json.loads(raw);f=p['fields'];year=f.get('tax_year')
        if isinstance(year,(int,float)) and not isinstance(year,bool) and int(year)==year and 1900<=year<=now.year and 0<=(now-instant(p['observed_at'])).total_seconds()<=7*86400:
            tax_rows.append(p)
    for f in ('annual_tax','tax_year'):
        selected.pop(f,None);provenance.pop(f,None);conflicts.pop(f,None)
    if tax_rows:
        year=max(p['fields']['tax_year'] for p in tax_rows)
        candidates=[p for p in tax_rows if p['fields']['tax_year']==year and p['fields'].get('annual_tax') is not None]
        selected['tax_year']=year
        if candidates:
            def priority(p):return 100 if p['source']['kind']=='mls' and p['source']['authoritative'] else 90 if p['source']['kind']=='public_records' and p['source']['authoritative'] else 70 if p['source']['kind']=='rentcast' else 50
            candidates.sort(key=lambda p:(priority(p),instant(p['observed_at'])),reverse=True);p=candidates[0]
            for f in ('annual_tax','tax_year'):
                selected[f]=p['fields'][f];provenance[f]=dict(value=p['fields'][f],source=p['source']['name'],kind='third_party_estimate' if f in p['estimates'] else 'verified_source',observed_at=p['observed_at'],priority=priority(p),source_kind=p['source']['kind'],record_id=p['record_id'],synthetic=p['source']['synthetic'])
            if any(c['fields']['annual_tax']!=p['fields']['annual_tax'] and (priority(c)>=priority(p) or instant(c['observed_at'])>=instant(p['observed_at'])) for c in candidates[1:]):
                conflicts['annual_tax']=[{'source':c['source']['name'],'value':c['fields']['annual_tax'],'year':year} for c in candidates]
    return dict(fields=selected,provenance=provenance,conflicts=conflicts,stale_evidence=stale,
        sources=sorted({json.loads(r[0])['source']['name'] for r in rows}),synthetic=any(json.loads(r[0])['source']['synthetic'] for r in rows))


def health(store,source,now,*,success=False,error=None,cursor=None):
    # Error category only; no raw exceptions, tokens, endpoints or private source records.
    store.execute('UPDATE discovery_sources SET last_attempt=?,last_success=CASE WHEN ?=1 THEN ? ELSE last_success END,last_error=?,cursor=CASE WHEN ?=1 THEN ? ELSE cursor END WHERE name=?',
      (now.isoformat(),int(success),now.isoformat(),error,int(success),cursor,source))
