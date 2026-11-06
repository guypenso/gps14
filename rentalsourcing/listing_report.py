"""Read-only RealScout section for the existing daily digest; never sends mail."""
import json
from .core import verification, geographic_verification
from .digest import recent_timestamp
from .investment_policy import annual_tax_passes
from .tax_backlog import review_deferred

def listing_report(store,boundary,now):
    rows=[json.loads(raw) for raw, in store.execute('SELECT payload FROM settings WHERE name LIKE ?',('listing_identity:%',)).fetchall()]
    cards=[]
    for r in rows:
        identity=r['source_id'];p=r['property']
        if review_deferred(identity,store) or not recent_timestamp(r['observed_at'],now):continue
        review=store.execute('SELECT payload FROM settings WHERE name=?',('penten_review:'+identity,)).fetchone()
        if review and json.loads(review[0]).get('status') not in (None,'new'):continue
        state,reasons=verification(p,boundary)
        if state=='Rejected' or geographic_verification(p,boundary)[0]!='Candidate':continue
        tax=r.get('tax_evidence');verified=r.get('tax_status')=='provider_verified' and tax and annual_tax_passes(tax['annual'])
        cards.append({'source_id':identity,'address':p['formattedAddress'],'price':p.get('price'),
            'qualification':'Qualified' if state=='Candidate' and verified else 'Pending Verification',
            'listing_url':r.get('listing_url'),'observed_at':r['observed_at'],'reasons':reasons})
    scheduler=store.execute('SELECT payload FROM settings WHERE name=?',('offmarket_scheduler',)).fetchone()
    job=json.loads(scheduler[0]) if scheduler else {}
    preflight=store.execute('SELECT payload FROM settings WHERE name=?',('listing_pre_report_receipt',)).fetchone()
    latest=json.loads(preflight[0]) if preflight else {}
    if latest.get('at','')>job.get('attempted_at',''):
        job={'attempted_at':latest['at'],'state':latest.get('result',{}).get('state','failed')}
    return {'properties':len(rows),'observations':store.execute('SELECT COUNT(*) FROM settings WHERE name LIKE ?',('listing_observation:%',)).fetchone()[0],
        'last_attempt_at':job.get('attempted_at'),'state':job.get('state','not_checked'),
        'last_observation':max((r['observed_at'] for r in rows),default=None),'cards':cards}

def section(data):
    import html
    lines=['RealScout — authorized listing alerts; source coverage is limited to received alerts.',
        f"Properties observed: {data['properties']} | Observations: {data['observations']} | Last intake attempt: {data['last_attempt_at']} | Intake status: {data['state']}"]
    for p in data['cards']:
        lines.append(f"{p['qualification']}: {p['address']} | Asking price: ${p['price']} | Observed: {p['observed_at']}\nListing: {p['listing_url'] or 'Not supplied'}")
    if not data['cards']:lines.append('No fresh, unreviewed RealScout candidates can be included from the available evidence. This does not establish zero market opportunities.')
    text='\n\n'.join(lines)
    return text,'<section><h2>RealScout</h2><pre style="white-space:pre-wrap;overflow-wrap:anywhere">'+html.escape(text)+'</pre></section>'

def sync_before_report(store,day,now,opener=None):
    """One bounded selected-sender intake in the existing email job, never RentCast."""
    from .command_center import review_configuration
    from .sync_runtime import request_json, component
    cfg=review_configuration()
    if not cfg:return {'state':'not_configured'}
    url,key,token=cfg
    body=json.dumps({'action':'listing_refresh','operation_id':'pre-report:'+day},separators=(',',':')).encode()
    def accepted(value,*args):
        if value.get('state') not in ('success','partial'):raise ValueError('Listing intake did not complete')
    try:
        result=request_json(store,'listing_pre_report',day,(url.rsplit('/',1)[0]+'/offmarket-refresh',key,token),body,
            timeout=90,opener=opener,now=now,validate=accepted)
    except Exception:
        store.save_setting('listing_pre_report_receipt',{'at':now.isoformat(),'day':day,'result':{'state':'failed'}})
        raise
    store.save_setting('listing_pre_report_receipt',{'at':now.isoformat(),'day':day,'result':result})
    component(store,'listing_pre_report',result['state'],now=now,pending=1 if result['state']=='partial' else 0)
    return result
