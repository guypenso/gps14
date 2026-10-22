"""Source-aware ranked report. Missing income/costs never become invented returns."""
import datetime as dt
import html
import json
from urllib.parse import quote
from .schedule import ISRAEL
from .realscout_store import decision,instant


def report(store,boundary,now,*,day=None,since=None,cutoff=None,public_url='',limit=200):
    day=day or now.astimezone(ISRAEL).date().isoformat()
    cutoff=cutoff or now.isoformat()
    since=since or dt.datetime.combine(dt.date.fromisoformat(day),dt.time.min,tzinfo=ISRAEL).astimezone(dt.timezone.utc).isoformat()
    rows=[]
    for cid,payload,first,last,effective,version,reviewed,reviewed_version in store.execute(
            'SELECT id,payload,first_seen,last_seen,effective_at,version,reviewed_at,reviewed_version FROM realscout_candidates ORDER BY first_seen,id').fetchall():
        p=json.loads(payload);d=decision(store,cid,boundary,now)
        observations=store.execute('SELECT o.discovery,o.payload,m.processed_at FROM realscout_observations o JOIN realscout_messages m ON m.id=o.message_id WHERE o.candidate_id=? ORDER BY o.sent_at DESC,o.message_id DESC',(cid,)).fetchall()
        latest=observations[0] if observations else None
        in_window=any(instant(since)<instant(o[2])<=instant(cutoff) for o in observations)
        events=[o for o in observations if instant(since)<instant(o[2])<=instant(cutoff) and o[0] in {'price_reduction','price_increase'}]
        discovery=events[0][0] if events else latest[0] if latest else 'unknown'
        previous=next((json.loads(o[1]).get('price') for o in observations[1:]
                       if json.loads(o[1]).get('price') is not None and json.loads(o[1]).get('price')!=p['price']),p.get('previous_price'))
        tax=d.get('tax');score=None;explanation='Unranked: insufficient verification or a failed hard criterion.'
        if d['state']=='Qualified' and tax:
            score=round(100*(2000-tax['adjusted'])/2000,2)
            explanation='Tax-headroom score = 100 × (2000 − adjusted annual tax) / 2000. Higher is better; this is not ROI.'
        pps=round(p['price']/p['square_footage'],2) if p['price'] is not None and p['square_footage'] else None
        link=public_url.rstrip('/')+'/realscout/detail/'+quote(cid,safe='') if public_url else None
        rows.append(dict(id=cid,address=f"{p['street']}, {p['city']}, {p['state']} {p['zipcode']}",price=p['price'],
            bedrooms=p['bedrooms'],bathrooms=p['bathrooms'],square_footage=p['square_footage'],
            listing_id=p.get('listing_id'),source='RealScout email alert',
            new_to_system=instant(since)<instant(first)<=instant(cutoff),seen_in_current_window=in_window,
            previously_reviewed=bool(reviewed),review_current=reviewed_version==version,
            discovery=discovery,price_changed=in_window and discovery in {'price_reduction','price_increase'},
            previous_price=previous,price_change_amount=round(p['price']-previous,2) if p['price'] is not None and previous is not None else None,
            source_label=p['alert_kind'],alert_profile=p.get('alert_profile','saved_search'),
            search_name=p.get('search_name'),days_on_site=p.get('days_on_site'),
            first_seen=first,last_received_at=last,email_observed_at=effective,version=version,
            state=d['state'],reasons=d['reasons'],tax=tax,score=score,score_explanation=explanation,
            price_per_sqft=pps,tax_to_price_pct=round(100*tax['adjusted']/p['price'],3) if tax and p['price'] else None,
            monthly_rent=None,gross_yield=None,net_yield=None,cash_flow=None,
            financial_limitations=['rent_not_available','operating_costs_and_financing_not_verified'],
            property_link=link,
            awaiting_financial_verification=d['state']=='Pending Verification' and bool(d.get('needs_tax') or any('tax' in r or 'financial' in r for r in d['reasons'])),
            awaiting_listing_verification=d['state']=='Pending Verification' and any(r in {'status','coordinates','stale_email_observation','identity_match_changed'} for r in d['reasons'])))
    rows.sort(key=lambda r:(not r['seen_in_current_window'],r['state']!='Qualified',
                             -(r['score'] if r['score'] is not None else -1),r['price_per_sqft'] or float('inf'),r['id']))
    counts=dict(total=len(rows),new_listings=sum(r['new_to_system'] for r in rows),
        price_changes=sum(r['price_changed'] for r in rows),previously_reviewed=sum(r['previously_reviewed'] for r in rows),
        awaiting_financial_verification=sum(r['awaiting_financial_verification'] for r in rows),
        awaiting_listing_verification=sum(r['awaiting_listing_verification'] for r in rows),
        qualified=sum(r['state']=='Qualified' for r in rows),rejected=sum(r['state']=='Rejected' for r in rows))
    last=store.execute('SELECT MAX(processed_at) FROM realscout_messages').fetchone()[0]
    arrived=store.execute('SELECT COUNT(*) FROM realscout_messages WHERE processed_at>? AND processed_at<=?',(since,cutoff)).fetchone()[0]
    from .realscout_worker import monitor
    health=monitor(store,now)
    return dict(day=day,coverage='alert_stream_only',fresh_inventory_scan_performed=False,
                transport_health='attention_required' if health['worker_stale'] or health['backlog_delayed'] or sum(health['queue'].get(k,0) for k in ['quarantined','pending','retry','processing']) else 'worker_healthy',
                health=health,last_processed_alert=last,new_alerts=arrived,since=since,cutoff=cutoff,
                rows=rows[:limit],omitted_rows=max(0,len(rows)-limit),counts=counts,
                ranking_policy='Qualified properties only: tax headroom descending, then price/sqft ascending. No rent/ROI assumption.')


def render_preview(data,*,preview=True):
    lines=[f"RentalSourcing — {data['day']}",
        'Discovery source: RealScout email alerts. This is not a complete MLS inventory scan.',
        f"New alerts ingested since the previous report window: {data['new_alerts']}.",
        'No new alerts were ingested in this report window; this is not a fresh MLS scan or proof of no market changes.' if not data['new_alerts'] else 'Newly ingested does not necessarily mean newly listed or fresh source evidence.',
        f"Last processed alert: {data['last_processed_alert'] or 'none'}; ingestion status: {data['transport_health']}.",
        f"New to system: {data['counts']['new_listings']} | Price changes: {data['counts']['price_changes']} | Previously reviewed: {data['counts']['previously_reviewed']} | Awaiting financial verification: {data['counts']['awaiting_financial_verification']} | Qualified: {data['counts']['qualified']}",
        data['ranking_policy']]
    if 'budget' in data:
        lines.append(f"App-approved API attempts remaining: {data['budget']['remaining']}; selective requests this run: {data['enrichment_requests_this_run']}. Provider-wide usage is not measured here.")
    for r in data['rows']:
        discovery='New to system' if r['new_to_system'] else ('Previously reviewed' if r['previously_reviewed'] else 'Previously seen, not reviewed')
        if r['price_changed']:discovery+='; '+r['discovery']
        lines.append(f"{r['address']} | ${r['price']:,.2f}" if r['price'] is not None else f"{r['address']} | Price unknown")
        lines.append(f"{r['bedrooms']} beds | {r['bathrooms']} baths | {r['square_footage']} sqft | source label: {r['source_label']} | listing ID: {r['listing_id'] or 'not supplied'}")
        lines.append(f"{discovery}; {r['state']}; reasons: {', '.join(r['reasons']) or 'all original checks passed'}")
        lines.append(f"Source observed: {r['email_observed_at']}; processed: {r['last_received_at']}; site days: {r['days_on_site']} (not verified MLS DOM)")
        lines.append(f"Score: {r['score'] if r['score'] is not None else 'unranked'}; {r['score_explanation']}")
        lines.append(f"Price/sqft: {r['price_per_sqft']}; adjusted annual tax/price %: {r['tax_to_price_pct']}")
        if r['tax']:lines.append(f"Latest reported annual tax ({r['tax']['year']}): ${r['tax']['annual']:,.2f} + $100 = ${r['tax']['adjusted']:,.2f}")
        lines.append('Rent, yield and cash flow: unavailable; no missing assumptions were invented.')
        if r['property_link']:lines.append('View original listing (dashboard login required): '+r['property_link'])
        else:lines.append('Original listing link retained encrypted; authenticated dashboard URL is not configured for this preview.')
    if data['omitted_rows']:lines.append(f"{data['omitted_rows']} additional historical rows omitted from this email; inspect the private dashboard.")
    text='\n'.join(lines)
    return dict(subject=f"RentalSourcing | {data['day']} | RealScout investment report"+(' — PREVIEW' if preview else ''),
                text=text,html='<html><body><pre style="white-space:pre-wrap">'+html.escape(text)+'</pre></body></html>')
