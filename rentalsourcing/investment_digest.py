"""Investor-facing change journal. Selection is independent of PENTEN delivery.

Canonical IDs come only from the authenticated destination feed. Frozen batches
advance their change watermark only after confirmed email acceptance. No network.
"""
import datetime as dt,hashlib,html,json,os,urllib.parse
from .sync_runtime import get
from .core import verification
from .collector import LIVE
from .digest import recent_timestamp

PREFIX='investment_digest:'

def money(value):return '$'+format(value,',.2f').removesuffix('.00')
def _hash(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def _time(value):
    try:
        d=dt.datetime.fromisoformat(value.replace('Z','+00:00'))
        return d.astimezone(dt.timezone.utc) if d.tzinfo else None
    except (ValueError,TypeError,AttributeError):return None

def _after(value,since,now):
    d=_time(value)
    return bool(d and since<d<=now)


def select(store,boundary,now):
    prior=store.execute("SELECT updated_at FROM outbox WHERE job_id LIKE ? AND state='sent' ORDER BY updated_at DESC LIMIT 1",('daily:%',)).fetchone()
    since=_time(prior[0]) if prior else now-dt.timedelta(days=1)
    estimates=get(store,'public_tax_batch:2026',{}).get('estimates',{})
    reviews={name[len('penten_review:'):]:json.loads(raw) for name,raw in store.execute("SELECT name,payload FROM settings WHERE name LIKE ?",('penten_review:%',)).fetchall()}
    # Current inventory only, never historical sync-operation bodies. Streaming
    # avoids an unbounded historical-ledger materialization.
    current={};candidates=[];missing_canonical=0
    cursor=store.execute('SELECT id,payload,state,observed_at FROM properties ORDER BY observed_at DESC,id')
    rows=[]
    while True:
        part=cursor.fetchmany(200)
        if not part:break
        rows.extend(part)
        if len(rows)>=2000:break
    source_rows=[(i,json.loads(raw),state,observed) for i,raw,state,observed in rows]
    # RealScout's durable identities are independent of RentCast capacity.
    for raw, in store.execute("SELECT payload FROM settings WHERE name LIKE ? ORDER BY name LIMIT 2000",('listing_identity:%',)).fetchall():
        r=json.loads(raw)
        if r.get('canonical_source_id') or not isinstance(r.get('property'),dict):continue
        source_rows.append((r['source_id'],r['property'],r.get('state'),r.get('observed_at')))
    first_seen={}
    def remember(identity, value):
        canonical=reviews.get(identity,{}).get('canonical_property_id')
        observed=_time(value)
        if canonical and observed and observed <= now:
            first_seen[canonical]=min(observed,first_seen.get(canonical,observed))
    for identity, first in store.execute('SELECT property_id,MIN(observed_at) FROM price_history GROUP BY property_id').fetchall():
        remember(identity,first)
    for prefix,field in (('first_discovery:%','at'),('listing_observation:%','observed_at')):
        identity="payload::jsonb->>'source_id'" if store.pg else "json_extract(payload,'$.source_id')"
        date=f"payload::jsonb->>'{field}'" if store.pg else f"json_extract(payload,'$.{field}')"
        for source_id,first in store.execute(f'SELECT {identity},MIN({date}) FROM settings WHERE name LIKE ? GROUP BY 1',(prefix,)).fetchall():
            remember(source_id,first)
    for source_id,p,state,observed in source_rows:
        remember(source_id,observed)
        review=reviews.get(source_id,{})
        if review.get('status')=='rejected' or review.get('conditional_watch',{}).get('active') or review.get('contract_state')=='terminated':continue
        if verification(p,boundary)[0]=='Rejected':continue
        canonical=review.get('canonical_property_id')
        if not canonical:missing_canonical+=1;continue
        # A shared destination ID prevents counting another source as a new card.
        old=current.get(canonical)
        if old and (_time(old['observed_at']) or dt.datetime.min.replace(tzinfo=dt.timezone.utc))>=(_time(observed) or dt.datetime.min.replace(tzinfo=dt.timezone.utc)):continue
        from .rental_intelligence import cached_estimate
        rent=cached_estimate(store,p,now)
        cached=store.get_tax(source_id,now.isoformat());tax=None
        from .provenance import demo_marked
        if cached and cached[1]==LIVE and cached[0].get('id')==source_id and not demo_marked(cached[0]):
            from .core import tax_verification
            tax_state,t,_=tax_verification(cached[0],now.date())
            if tax_state=='Qualified' and t:tax={'state':'verified','annual':t['annual'],'year':t['year'],'observed_at':cached[2]}
        estimate=estimates.get(source_id,{})
        if not tax and estimate.get('state')=='estimated' and estimate.get('match_status')=='unique_parcel' and estimate.get('sources'):tax={'state':'estimated','annual':estimate['annual'],'year':estimate['year'],'observed_at':estimate.get('retrieved_at')}
        facts={'rent':rent['monthly'] if rent else None,'tax':{k:tax[k] for k in ('state','annual','year')} if tax else None,'status':p.get('status')}
        current[canonical]={'source_id':source_id,'canonical':canonical,'address':p.get('formattedAddress'),
            'price':p.get('price'),'observed_at':observed,'review':review,'rent':rent,'tax':tax,'facts':facts}
    for canonical,c in current.items():
        review=c['review'];baseline=get(store,PREFIX+'baseline:'+canonical,{})
        base={'canonical':canonical,'address':c['address'],'price':c['price'],'source_id':c['source_id']}
        def add(kind,proof,detail):
            candidates.append({**base,'kind':kind,'detail':detail,'event_id':_hash([canonical,kind,proof])})
        watch=review.get('conditional_watch') or {}
        if (review.get('status')=='new' and _after(review.get('discovered_at'),since,now)
                and first_seen.get(canonical, since)>since and recent_timestamp(c['observed_at'],now)):
            add('new',canonical,'Asking price: '+money(c['price']) if isinstance(c['price'],(int,float)) else '')
        if watch.get('state')=='triggered' and review.get('status')=='new' and _after(watch.get('reactivated_at'),since,now):
            add('reactivated',watch.get('cycle_id'),f"Target reached: {money(watch['last_price_cents']/100)} · Target: {money(watch['target_cents']/100)}")
        if review.get('status') in ('pursue','monitor'):
            changes=store.execute('SELECT previous_price,price,observed_at FROM price_history WHERE property_id=? AND observed_at>? AND reduced=1 ORDER BY observed_at DESC LIMIT 1',(c['source_id'],since.isoformat())).fetchone()
            if changes and c['facts']['status']=='Active' and recent_timestamp(c['observed_at'],now) and c['price']==changes[1]:
                add('price',[changes[0],changes[1],changes[2]],f"Asking price: {money(changes[0])} → {money(changes[1])}")
        for kind,evidence in [('rent',c['rent']),('tax',c['tax'])]:
            if evidence and _after(evidence.get('observed_at'),since,now) and baseline.get(kind)!=c['facts'][kind]:
                detail=('Estimated monthly rent: '+money(evidence['monthly'])+' · RentCast' if kind=='rent' else
                        ('Verified annual tax' if evidence['state']=='verified' else 'Estimated annual tax')+': '+money(evidence['annual'])+' · '+str(evidence['year']))
                add('data',[kind,c['facts'][kind]],detail)
        if (baseline.get('status') and c['facts']['status'] in {'Active','Inactive','Pending','Under Contract','Sold','Withdrawn','Expired','Off Market'}
                and baseline['status']!=c['facts']['status'] and _after(c['observed_at'],since,now)):
            add('status',[baseline['status'],c['facts']['status'],c['observed_at']],str(baseline['status'])+' → '+str(c['facts']['status']))
    selected=[]
    for c in sorted(candidates,key=lambda c:(['new','reactivated','price','data','status'].index(c['kind']),c['canonical'])):
        key=PREFIX+'event:'+c['event_id'];event=get(store,key,{})
        if event.get('sent_at'):continue
        if event.get('queued_outbox'):
            queued=store.execute('SELECT state FROM outbox WHERE id=?',(event['queued_outbox'],)).fetchone()
            if queued and queued[0] not in ('failed','cancelled','cancelled_stale','suppressed'):continue
        if not any(x['event_id']==c['event_id'] for x in selected):selected.append(c)
    return {'prepared_at':now.isoformat(),'updates':selected[:100],'more':len(selected)>100,'since':since.isoformat(),
            'baselines':{i:c['facts'] for i,c in current.items()},'missing_canonical':missing_canonical,
            'provider_calls':0,'email_sends':0}


def render(day,plan,stats,origin):
    updates=plan['updates'];new={c['canonical'] for c in updates if c['kind'] in ('new','reactivated')}
    date=dt.date.fromisoformat(day).strftime('%d %b %Y')
    subject=f'RentalSourcing | {date} | '+(str(len(new))+' New Opportunities' if new else 'Investment Updates' if updates else 'No New Opportunities')
    fresh=bool(stats.get('fresh_request_made') and stats.get('pages') and stats.get('requests_consumed',0)>0)
    names={'pittsburgh-bellevue':'Pittsburgh / Bellevue query','bethel-park':'Bethel Park query','pleasant-hills':'Pleasant Hills query'}
    areas=list(dict.fromkeys(names.get(p['partition'],p['partition']) for p in stats.get('pages',[])))
    if fresh:coverage='Fresh RentCast retrieval: '+', '.join(areas)+f". {stats.get('received_this_run',0)} listing records returned. Query coverage is partial; full municipal coverage is unverified."
    elif stats.get('discovery_state')=='scheduled_pacing_no_listing_request':coverage='No fresh RentCast query this window. Monthly pacing reserved requests for later windows.'
    else:coverage='No fresh RentCast results were verified in this window.'
    free=stats.get('free_source',{})
    if free.get('mailbox_checked',free.get('checked')):
        coverage+=' RealScout alerts were checked.'
    elif any(reason.startswith('gmail_') for reason in free.get('blocked',[])):
        coverage+=' RealScout is waiting for Gmail access.'
    else:
        coverage+=' A RealScout check could not be verified.'
    if free.get('public_inventory_state')=='fetched':coverage+=' Pittsburgh Land Bank inventory was checked.'
    if free.get('public_inventory_state')=='recent_verified':coverage+=' A recent verified Pittsburgh Land Bank check was reused.'
    if stats.get('discovery_state')=='skipped_budget_exhausted':coverage+=' RentCast retrieval was blocked by approved capacity or authorization.'
    if stats.get('discovery_state')=='failed':coverage+=' Source retrieval failed; recovery remains pending.'
    lines=['RentalSourcing — '+date];parts=[]
    for title,kinds in [('New Opportunities',('new','reactivated')),('Important Changes',('price','status')),('Newly Available Investment Data',('data',))]:
        cards=[c for c in updates if c['kind'] in kinds]
        if not cards:continue
        lines.append(title);markup=[]
        for c in cards:
            link=origin+'/?'+urllib.parse.urlencode({'view':'rentalsourcing','opportunity':c['canonical'],'tab':'details'}) if origin else ''
            lines.append(c['address']+'\n'+c['detail']+('\n'+link if link else ''))
            address=html.escape(c['address'])
            heading='<a style="color:#165344;font-weight:700" href="'+html.escape(link,quote=True)+'">'+address+'</a>' if link else '<strong>'+address+'</strong>'
            markup.append('<article style="padding:14px 0;border-bottom:1px solid #dce6e2">'+heading+'<p>'+html.escape(c['detail'])+'</p></article>')
        parts.append('<section><h2 style="font-size:18px">'+title+'</h2>'+''.join(markup)+'</section>')
    if not updates:lines.append('No new actionable investment updates since the previous report.')
    if plan['more']:lines.append('Showing the first 100 updates; additional records remain available in PENTEN.')
    lines+=['Scan Coverage',coverage]
    if origin:lines.append('Open PENTEN: '+origin+'/?view=rentalsourcing')
    markup='<html lang="en"><head><meta name="viewport" content="width=device-width,initial-scale=1"></head><body style="font-family:Arial,sans-serif;line-height:1.55;color:#173b36;max-width:680px;margin:auto;padding:24px"><h1 style="font-size:24px">RentalSourcing</h1><p>'+html.escape(date)+'</p>'+''.join(parts)
    if not updates:markup+='<p>'+html.escape(lines[1])+'</p>'
    markup+='<section><h2 style="font-size:18px">Scan Coverage</h2><p>'+html.escape(coverage)+'</p></section>'
    if origin:markup+='<p><a href="'+html.escape(origin+'/?view=rentalsourcing',quote=True)+'">Open PENTEN</a></p>'
    markup+='</body></html>'
    return subject,'\n\n'.join(lines),markup


def freeze(store,outbox_id,plan):
    store.save_setting(PREFIX+'batch:'+outbox_id,plan)
    for event in plan['updates']:
        store.save_setting(PREFIX+'event:'+event['event_id'],{**event,'queued_outbox':outbox_id})


def acknowledge(store,outbox_id,now):
    row=store.execute("SELECT state,provider_id FROM outbox WHERE id=?",(outbox_id,)).fetchone()
    if not row or row[0]!='sent' or not row[1]:return False
    plan=get(store,PREFIX+'batch:'+outbox_id,{})
    for event in plan.get('updates',[]):
        key=PREFIX+'event:'+event['event_id'];old=get(store,key,{})
        store.save_setting(key,{**old,'sent_at':now.isoformat(),'provider_confirmed':True})
    for canonical,facts in plan.get('baselines',{}).items():
        key=PREFIX+'baseline:'+canonical;old=get(store,key,{})
        if old.get('_at','')<=plan.get('prepared_at',''):store.save_setting(key,{**facts,'_at':plan.get('prepared_at')})
    return True
