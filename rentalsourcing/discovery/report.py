"""One source-aware report window through the existing durable daily outbox."""
import datetime as dt
import hashlib,html,json
from ..schedule import ISRAEL
from ..digest import Digest,ReconciliationRequired
from .evaluate import evaluate,screen
from .models import instant


def scan_outcomes(data):
    """Current-run facts only; a cache hit or inbox bridge is never a fresh scan."""
    outcomes=[]
    for row in data.get('source_results',[]):
        errors=[row.get('error'),row.get('enrichment_error')]+row.get('enrichment',{}).get('errors',[])
        if any(e in {'BudgetExceeded','approved_budget_unavailable'} for e in errors):state='internal_api_budget_exhausted'
        elif row.get('error'):state='source_unavailable' if not row.get('records') else 'partial_scan_failed'
        elif row.get('enrichment_error') or row.get('enrichment',{}).get('errors'):state='financial_verification_incomplete'
        elif 'enrichment' in row or 'point_verification' in row:state='verification_only'
        elif row.get('mode')=='cached_provider_page':state='cached_results_only'
        elif row.get('fresh_scan') or row.get('mode')=='licensed_api' and row.get('complete'):
            state='fresh_discovery' if row.get('all_pages_consumed',True) else 'fresh_partial_discovery'
        elif row.get('mode')=='authorized_export':state='export_imported_snapshot_time_preserved'
        elif row.get('mode')=='ingested_alert_bridge':state='new_alerts_ingested' if row.get('records') else 'no_new_alerts'
        else:state='scan_skipped'
        requests=row.get('requests',row.get('enrichment',row.get('point_verification',{})).get('requests',0))
        outcomes.append(dict(source=row.get('source','unknown'),state=state,records=row.get('records'),requests=requests))
    return outcomes


def snapshot(store,boundary,now,*,day=None,since=None,assumptions=None,financing=None,limit=200,dual_mode=False,loan_scenarios=None,property_inputs=None,ranking_weights=None,ranking_approval_id='',primary_financing_scenario='base',property_loan_scenarios=None,report_mode=None):
    if report_mode=='mvp':dual_mode=False
    day=day or now.astimezone(ISRAEL).date().isoformat()
    if since is None:
        prior=store.execute("SELECT d.cutoff,d.snapshot FROM discovery_reports d JOIN outbox o ON o.id=d.outbox_id WHERE o.state='sent' AND d.day<? ORDER BY d.day DESC LIMIT 1",(day,)).fetchone()
        since=prior[0] if prior else dt.datetime.combine(dt.date.fromisoformat(day),dt.time.min,tzinfo=ISRAEL).astimezone(dt.timezone.utc).isoformat()
        previous=(json.loads(prior[1]).get('all_states') or {p['id']:p['status'] for p in json.loads(prior[1])['properties']}) if prior else {}
    else:previous={}
    properties=[]
    for eid,first,version in store.execute('SELECT id,first_seen,version FROM discovery_entities').fetchall():
        from .dual import merge_loan_profiles
        selected_loans=merge_loan_profiles(loan_scenarios,(property_loan_scenarios or {}).get(eid,{})) if dual_mode else loan_scenarios
        item=screen(store,eid,boundary,now) if report_mode=='mvp' else evaluate(store,eid,boundary,now,assumptions=assumptions,financing=financing,dual_mode=dual_mode,loan_scenarios=selected_loans,property_inputs=(property_inputs or {}).get(eid,{}))
        events=store.execute('SELECT kind,source,payload,processed_at FROM discovery_observations WHERE entity_id=? AND processed_at>? AND processed_at<=? ORDER BY processed_at DESC,id DESC',(eid,since,now.isoformat())).fetchall()
        item.update(new=instant(since)<instant(first)<=now,price_reduction=any(e[0]=='price_reduction' and (report_mode!='mvp' or json.loads(e[2])['fields'].get('price')==item['fields'].get('price')) for e in events),
            changed=any(e[0] in {'facts_changed','price_increase','price_reduction'} for e in events),seen_this_window=bool(events),
            newly_qualified=item['status']=='Qualified' and previous.get(eid)!='Qualified' and (eid in previous or any(e[0] in {'additional_source','facts_changed'} for e in events)),previously_reported=eid in previous)
        links=[];ids=[];comps=[]
        for (raw,) in store.execute('SELECT payload FROM discovery_records WHERE entity_id=?',(eid,)).fetchall():
            p=json.loads(raw)
            if p.get('url'):links.append({'source':p['source']['name'],'url':p['url']})
            ids.append({'source':p['source']['name'],'record_id':p['record_id'],'mls_id':p.get('mls_id'),'mls_system':p.get('mls_system')})
            comps+=p.get('comparables',[])
        item.update(links=links,source_identifiers=ids,rental_comparables=comps[:25])
        item['recommendation']=item['status']=='Qualified' and not ((dual_mode or report_mode=='mvp') and item['synthetic']) and (item['new'] or item['price_reduction'] or item['newly_qualified'])
        properties.append(item)
    def priority(p):
        if report_mode=='mvp':
            group=0 if p['status']=='Qualified' and p['new'] else 1 if p['status']=='Qualified' and (p['price_reduction'] or p['newly_qualified']) else 2 if p['status']=='Pending Verification' and p['seen_this_window'] else 3
            return group,-instant(p['discovered_at']).timestamp(),p['id']
        group=0 if p['recommendation'] and p['new'] else 1 if p['recommendation'] else 2 if p['status']=='Pending Verification' and p['seen_this_window'] else 3 if p['changed'] else 4
        return group,-(p['investment_score'] if p['investment_score'] is not None else -999),p['id']
    properties.sort(key=priority)
    sources=[]
    for name,payload,last,attempt,error,cursor in store.execute('SELECT name,payload,last_success,last_attempt,last_error,cursor FROM discovery_sources').fetchall():
        p=json.loads(payload);sources.append(dict(synthetic=p['synthetic'],name=name,kind=p['kind'],enabled=p['enabled'],scope=p['scope'],last_success=last,last_attempt=attempt,error=error,
            stale=not last or now-instant(last)>dt.timedelta(hours=24),mode='alert_stream' if p['kind']=='realscout' else 'licensed_feed_or_export' if p['kind']=='mls' else 'provider_or_record_evidence'))
    counts=dict(total=len(properties),new=sum(p['new'] for p in properties),price_reductions=sum(p['price_reduction'] for p in properties),
        qualified=sum(p['status']=='Qualified' for p in properties),pending=sum(p['status']=='Pending Verification' for p in properties),
        rejected=sum(p['status']=='Rejected' for p in properties),recommendations=sum(p['recommendation'] for p in properties),
        seen_this_window=sum(p['seen_this_window'] for p in properties))
    mls=[s for s in sources if s['kind']=='mls' and s['enabled'] and not s['synthetic']]
    from .ranking import rank
    rankings=rank(properties,weights=ranking_weights) if dual_mode else None
    return dict(report_mode=report_mode or ('advanced' if dual_mode else 'legacy'),underwriting_version='mvp-v1' if report_mode=='mvp' else 'dual-v1' if dual_mode else 'legacy-v1',rankings=rankings,ranking_approval_id=ranking_approval_id,primary_financing_scenario=primary_financing_scenario,all_states={p['id']:p['status'] for p in properties},day=day,since=since,cutoff=now.isoformat(),properties=properties[:limit],omitted=max(0,len(properties)-limit),counts=counts,sources=sources,
        authorized_mls_connected=bool(mls and any(s['last_success'] and not s['error'] and not s['stale'] for s in mls)),
        coverage_pct=None,coverage_statement='No authorized real MLS benchmark has been measured. Incremental checks and alert events are not full-inventory scans.',
        no_new_listings=not counts['new'],synthetic=any(p['synthetic'] for p in properties),
        score_policy='Original hard-filter screening only; no investment ranking.' if report_mode=='mvp' else 'Separate cash yield/monthly-flow and financed CoC/monthly-flow cohort-percentile scores; proposed equal weights, no combined score or profitability cutoff.' if dual_mode else 'Investment score = modeled annual unlevered after-reserve yield percentage points. No minimum ROI threshold was recovered or invented.')


def render(data):
    if data.get('report_mode')=='mvp':
        from .mvp_report import render as render_mvp
        return render_mvp(data)
    if data.get('underwriting_version')=='dual-v1':
        from .dual_report import render as render_dual
        return render_dual(data)
    lines=[f"RentalSourcing | {data['day']}",
        'SYNTHETIC OFFLINE DEMONSTRATION — NOT REAL INVESTMENTS' if data['synthetic'] else 'Private investment screening; modeled estimates/assumptions are explicitly labeled.',
        data['coverage_statement'],
        'No new listings were discovered in this report window. Stored listings below are not new scans.' if data['no_new_listings'] else f"Newly discovered properties: {data['counts']['new']} (source observation dates remain authoritative).",
        f"Evaluated: {data['counts']['total']} | Qualified original screening + complete unlevered model: {data['counts']['qualified']} | Pending: {data['counts']['pending']} | Price reductions: {data['counts']['price_reductions']} | New recommendations: {data['counts']['recommendations']}",
        data['score_policy'], 'Configured MLS feed returned current data (not a licensing/coverage acceptance decision): '+str(data['authorized_mls_connected'])]
    for source in data['sources']:
        lines.append(f"Source {source['name']} ({source['kind']}): last successful data check/export={source['last_success'] or 'never'}; current error={source['error'] or 'none'}; mode={source['mode']}; stale={source['stale']}")
    outcomes=scan_outcomes(data)
    lines.append('Discovery run: '+('no discovery attempt recorded' if not outcomes else '; '.join(r['source']+': '+r['state'] for r in outcomes)))
    if 'api' in data:lines.append('RentCast app budget: '+json.dumps(data['api'],sort_keys=True)+'. Other applications/account usage is not observed here.')
    for p in data['properties']:
        f=p['fields'];m=p['financial']['metrics'];category='NEW + PRICE REDUCTION' if p['new'] and p['price_reduction'] else 'NEW' if p['new'] else 'PRICE REDUCTION' if p['price_reduction'] else 'NEWLY QUALIFIED' if p['newly_qualified'] else 'CHANGE' if p['changed'] else 'PREVIOUSLY SEEN'
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
    cells=[]
    def amount(value):return f'{value:,.2f}' if isinstance(value,(int,float)) and not isinstance(value,bool) else 'Unverified'
    for p in data['properties']:
        m=p['financial']['metrics'];f=p['fields']
        link=p['links'][0]['url'] if p['links'] else None
        address=html.escape(p['address'])
        if link:address='<a href="'+html.escape(link,quote=True)+'">'+address+'</a>'
        activity=', '.join(label for flag,label in ((p['new'],'New'),(p['price_reduction'],'Price reduction'),(p['newly_qualified'],'Newly qualified')) if flag) or ('Changed' if p['changed'] else 'Previously seen')
        values=[address,html.escape(activity),amount(f.get('price')),html.escape(p['status']),amount(m.get('cash_flow_monthly')),amount(p['investment_score']),amount(p['evidence_confidence_pct']),html.escape(', '.join(p['reasons']) or 'Screening passed; review assumptions')]
        cells.append('<tr>'+''.join('<td style="padding:8px;border:1px solid #ddd">'+v+'</td>' for v in values)+'</tr>')
    table='<table style="border-collapse:collapse"><thead><tr>'+''.join('<th>'+v+'</th>' for v in ('Property','Activity','Asking price ($)','Qualification','Monthly cash flow ($)','Net-yield score (%)','Evidence indicator (%)','Reasons / missing evidence'))+'</tr></thead><tbody>'+''.join(cells)+'</tbody></table>'
    return dict(subject=f"RentalSourcing | {data['day']} | {data['counts']['recommendations']} new investment candidates",text=text,
        html='<html><body><h1>RentalSourcing</h1><p>'+html.escape(lines[1])+'</p><p>'+html.escape('Discovery: '+('; '.join(r['source']+': '+r['state'] for r in outcomes) or 'no discovery attempt recorded'))+'</p>'+table+'<h2>Evidence and system status</h2><pre style="white-space:pre-wrap">'+html.escape(text)+'</pre></body></html>')


def prepare(store,boundary,settings,cfg,day,now,*,source_results=None):
    if not cfg.enabled or not cfg.daily_enabled or settings.discovery_source!='unified':raise PermissionError('Unified reports disabled')
    key='daily:'+day;identity='daily-'+hashlib.sha256(key.encode()).hexdigest()
    with store.transaction():
        if store.pg:store.execute('SELECT pg_advisory_xact_lock(73541209)')
        old=store.execute('SELECT id,state,first_attempt_at,event_ids FROM outbox WHERE job_id=?',(key,)).fetchone()
        if old and (old[2] or old[1] in {'sent','failed','uncertain','sending'}):return old[0]
        data=snapshot(store,boundary,now,day=day,assumptions=cfg.assumptions,financing=cfg.financing,dual_mode=cfg.dual_enabled and cfg.report_mode=='advanced',report_mode=cfg.report_mode,loan_scenarios=cfg.financing_scenarios,property_inputs=cfg.property_inputs,ranking_weights=cfg.ranking_weights,ranking_approval_id=cfg.ranking_approval_id,primary_financing_scenario=cfg.primary_financing_scenario,property_loan_scenarios=cfg.property_financing_scenarios)
        data['source_results']=source_results or []
        data['scan_outcomes']=scan_outcomes(data)
        data['api']=store.budget_health(settings,now,record_warning=False)
        body=render(data);body.update({'from':settings.email_from,'to':[settings.email_to]})
        data['outbox_payload_sha256']=hashlib.sha256(json.dumps(body).encode()).hexdigest()
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
    if data.get('report_mode')=='mvp':
        if cfg.report_mode!='mvp':return False
        for old in data['properties']:
            current=screen(store,old['id'],boundary,now)
            if any(current[key]!=old[key] for key in ('version','status','tax','fields','reasons')):return False
        return True
    dual=data.get('underwriting_version')=='dual-v1'
    if dual and (not cfg.dual_enabled or data.get('rankings',{}).get('weights')!=cfg.ranking_weights or data.get('ranking_approval_id','')!=cfg.ranking_approval_id or data.get('primary_financing_scenario')!=cfg.primary_financing_scenario):return False
    for old in data['properties']:
        current=evaluate(store,old['id'],boundary,now,assumptions=cfg.assumptions,financing=cfg.financing,dual_mode=dual,loan_scenarios=cfg.loans_for(old['id']),property_inputs=cfg.property_inputs.get(old['id'],{}))
        if dual and (current['acquisition']!=old.get('acquisition') or current['status']!=old['status']):return False
        if current['version']!=old['version']:return False
        if old['status']=='Qualified' and (current['status']!='Qualified' or current['tax']!=old['tax'] or current['financial']!=old['financial']):return False
    return True
