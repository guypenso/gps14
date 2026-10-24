"""One source-aware report window through the existing durable daily outbox."""
import datetime as dt
import hashlib,html,json
from ..schedule import ISRAEL
from ..digest import Digest,ReconciliationRequired
from .evaluate import evaluate
from .models import instant


def snapshot(store,boundary,now,*,day=None,since=None,assumptions=None,financing=None,limit=200):
    day=day or now.astimezone(ISRAEL).date().isoformat()
    if since is None:
        prior=store.execute("SELECT d.cutoff,d.snapshot FROM discovery_reports d JOIN outbox o ON o.id=d.outbox_id WHERE o.state='sent' AND d.day<? ORDER BY d.day DESC LIMIT 1",(day,)).fetchone()
        since=prior[0] if prior else dt.datetime.combine(dt.date.fromisoformat(day),dt.time.min,tzinfo=ISRAEL).astimezone(dt.timezone.utc).isoformat()
        previous=(json.loads(prior[1]).get('all_states') or {p['id']:p['status'] for p in json.loads(prior[1])['properties']}) if prior else {}
    else:previous={}
    properties=[]
    for eid,first,version in store.execute('SELECT id,first_seen,version FROM discovery_entities').fetchall():
        item=evaluate(store,eid,boundary,now,assumptions=assumptions,financing=financing)
        events=store.execute('SELECT kind,source,payload,processed_at FROM discovery_observations WHERE entity_id=? AND processed_at>? AND processed_at<=? ORDER BY processed_at DESC,id DESC',(eid,since,now.isoformat())).fetchall()
        item.update(new=instant(since)<instant(first)<=now,price_reduction=any(e[0]=='price_reduction' for e in events),
            changed=any(e[0] in {'facts_changed','price_increase','price_reduction'} for e in events),seen_this_window=bool(events),
            newly_qualified=item['status']=='Qualified' and previous.get(eid)!='Qualified' and (eid in previous or any(e[0] in {'additional_source','facts_changed'} for e in events)),previously_reported=eid in previous)
        links=[];ids=[];comps=[]
        for (raw,) in store.execute('SELECT payload FROM discovery_records WHERE entity_id=?',(eid,)).fetchall():
            p=json.loads(raw)
            if p.get('url'):links.append({'source':p['source']['name'],'url':p['url']})
            ids.append({'source':p['source']['name'],'record_id':p['record_id'],'mls_id':p.get('mls_id'),'mls_system':p.get('mls_system')})
            comps+=p.get('comparables',[])
        item.update(links=links,source_identifiers=ids,rental_comparables=comps[:25])
        item['recommendation']=item['status']=='Qualified' and (item['new'] or item['price_reduction'] or item['newly_qualified'])
        properties.append(item)
    properties.sort(key=lambda p:(not p['recommendation'],not p['price_reduction'],not(p['status']=='Pending Verification' and p['seen_this_window']),not p['changed'],not p['new'],-(p['investment_score'] if p['investment_score'] is not None else -999),p['id']))
    sources=[]
    for name,payload,last,attempt,error,cursor in store.execute('SELECT name,payload,last_success,last_attempt,last_error,cursor FROM discovery_sources').fetchall():
        p=json.loads(payload);sources.append(dict(synthetic=p['synthetic'],name=name,kind=p['kind'],enabled=p['enabled'],scope=p['scope'],last_success=last,last_attempt=attempt,error=error,
            stale=not last or now-instant(last)>dt.timedelta(hours=24),mode='alert_stream' if p['kind']=='realscout' else 'licensed_feed_or_export' if p['kind']=='mls' else 'provider_or_record_evidence'))
    counts=dict(total=len(properties),new=sum(p['new'] for p in properties),price_reductions=sum(p['price_reduction'] for p in properties),
        qualified=sum(p['status']=='Qualified' for p in properties),pending=sum(p['status']=='Pending Verification' for p in properties),
        rejected=sum(p['status']=='Rejected' for p in properties),recommendations=sum(p['recommendation'] for p in properties),
        seen_this_window=sum(p['seen_this_window'] for p in properties))
    mls=[s for s in sources if s['kind']=='mls' and s['enabled'] and not s['synthetic']]
    return dict(all_states={p['id']:p['status'] for p in properties},day=day,since=since,cutoff=now.isoformat(),properties=properties[:limit],omitted=max(0,len(properties)-limit),counts=counts,sources=sources,
        authorized_mls_connected=bool(mls and any(s['last_success'] and not s['error'] and not s['stale'] for s in mls)),
        coverage_pct=None,coverage_statement='No authorized real MLS benchmark has been measured. Incremental checks and alert events are not full-inventory scans.',
        no_new_listings=not counts['new'],synthetic=any(p['synthetic'] for p in properties),
        score_policy='Investment score = modeled annual unlevered after-reserve yield percentage points. No minimum ROI threshold was recovered or invented.')


def render(data):
    lines=[f"RentalSourcing | {data['day']}",
        'SYNTHETIC OFFLINE DEMONSTRATION — NOT REAL INVESTMENTS' if data['synthetic'] else 'Private investment screening; modeled estimates/assumptions are explicitly labeled.',
        data['coverage_statement'],
        'No new listings were discovered in this report window. Stored listings below are not new scans.' if data['no_new_listings'] else f"Newly discovered properties: {data['counts']['new']} (source observation dates remain authoritative).",
        f"Evaluated: {data['counts']['total']} | Qualified original screening + complete unlevered model: {data['counts']['qualified']} | Pending: {data['counts']['pending']} | Price reductions: {data['counts']['price_reductions']} | New recommendations: {data['counts']['recommendations']}",
        data['score_policy'], 'Authorized MLS connection verified by a successful configured feed/export: '+str(data['authorized_mls_connected'])]
    for source in data['sources']:
        lines.append(f"Source {source['name']} ({source['kind']}): last successful data check/export={source['last_success'] or 'never'}; current error={source['error'] or 'none'}; mode={source['mode']}; stale={source['stale']}")
    if data.get('source_results'):lines.append('Actual source work this run: '+json.dumps(data['source_results'],sort_keys=True))
    if 'api' in data:lines.append('RentCast app budget: '+json.dumps(data['api'],sort_keys=True)+'. Other applications/account usage is not observed here.')
    for p in data['properties']:
        f=p['fields'];m=p['financial']['metrics'];category='NEW' if p['new'] else 'PRICE REDUCTION' if p['price_reduction'] else 'NEWLY QUALIFIED' if p['newly_qualified'] else 'CHANGE' if p['changed'] else 'PREVIOUSLY SEEN'
        lines += [f"{category} | {p['address']} | ${f.get('price')} | {p['status']}",
          f"{f.get('bedrooms')} beds / {f.get('bathrooms')} baths / {f.get('squareFootage')} sqft / {f.get('propertyType') or 'type unverified'}",
          f"Discovered {p['discovered_at']}; latest evidence {p['latest_verification']}; sources {', '.join(p['sources'])}",
          f"Investment score {p['investment_score']} | Completeness {p['data_completeness_pct']}% | Evidence indicator {p['evidence_confidence_pct']}% (not a probability) | Financial confidence: {p['financial_confidence']}",
          'Financial metrics: '+json.dumps(m,sort_keys=True),
          'Inputs/provenance: '+json.dumps(p['financial'].get('inputs',{}),sort_keys=True),
          'Reasons: '+(', '.join(p['reasons']) or 'Original hard rules passed; financial model complete; assumptions/estimates are not guarantees.'),
          'Risks: '+', '.join(p['risks']), 'Next research: '+p['next_research']]
        lines += [link['source']+': '+link['url'] for link in p['links']]
        if not p['links']:lines.append('Listing URL not supplied; use source listing identifiers in the private report.')
        if p['rental_comparables']:lines.append('Provider rental comparisons (not independent verification): '+json.dumps(p['rental_comparables']))
    if data['omitted']:lines.append(f"{data['omitted']} additional records remain in the existing database/private report.")
    text='\n'.join(lines)
    return dict(subject=f"RentalSourcing | {data['day']} | {data['counts']['recommendations']} new investment candidates",text=text,
        html='<html><body><pre style="white-space:pre-wrap">'+html.escape(text)+'</pre></body></html>')


def prepare(store,boundary,settings,cfg,day,now,*,source_results=None):
    if not cfg.enabled or not cfg.daily_enabled or settings.discovery_source!='unified':raise PermissionError('Unified reports disabled')
    key='daily:'+day;identity='daily-'+hashlib.sha256(key.encode()).hexdigest()
    with store.transaction():
        if store.pg:store.execute('SELECT pg_advisory_xact_lock(73541209)')
        old=store.execute('SELECT id,state,first_attempt_at,event_ids FROM outbox WHERE job_id=?',(key,)).fetchone()
        if old and (old[2] or old[1] in {'sent','failed','uncertain','sending'}):return old[0]
        data=snapshot(store,boundary,now,day=day,assumptions=cfg.assumptions,financing=cfg.financing)
        data['source_results']=source_results or []
        data['api']=store.budget_health(settings,now,record_warning=False)
        body=render(data);body.update({'from':settings.email_from,'to':[settings.email_to]})
        if old:
            for event_id in json.loads(old[3]):store.execute("UPDATE deal_events SET state='ready' WHERE id=? AND state='queued'",(event_id,))
        store.execute("INSERT INTO outbox VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET state='pending',payload=excluded.payload,event_ids='[]',updated_at=excluded.updated_at WHERE outbox.first_attempt_at IS NULL AND outbox.state IN ('pending','cancelled')",
            (identity,key,'pending',json.dumps(body),'[]',None,None,now.isoformat()))
        store.execute('INSERT INTO discovery_reports VALUES (?,?,?,?) ON CONFLICT(outbox_id) DO UPDATE SET snapshot=excluded.snapshot,cutoff=excluded.cutoff',
            (identity,day,json.dumps(data),data['cutoff']))
    return identity


def validate_frozen(store,boundary,identity,now,cfg):
    row=store.execute('SELECT snapshot FROM discovery_reports WHERE outbox_id=?',(identity,)).fetchone()
    if not row:return True
    data=json.loads(row[0])
    if data['synthetic']:raise PermissionError('Synthetic unified reports cannot be sent')
    for old in data['properties']:
        current=evaluate(store,old['id'],boundary,now,assumptions=cfg.assumptions,financing=cfg.financing)
        if current['version']!=old['version']:return False
        if old['status']=='Qualified' and (current['status']!='Qualified' or current['tax']!=old['tax'] or current['financial']!=old['financial']):return False
    return True
