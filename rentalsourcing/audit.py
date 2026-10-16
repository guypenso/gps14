"""Read-only database audit; no migrations, provider requests or email."""
import datetime as dt
import json
import os
from collections import Counter
from .collector import LIVE
from .config import Settings
from .core import verification, tax_verification
from .database import validate_postgres_url
from .geography import load_boundary
from .provenance import demo_marked
from .storage import Store


def analyze(store,boundary,settings,now=None):
    now=now or dt.datetime.now(dt.timezone.utc)
    states=Counter();pending=Counter();reasons=Counter();eligible=0
    rows=store.execute('SELECT id,payload,state,reasons,source,observed_at FROM properties').fetchall()
    for identity,payload,state,why,source,observed in rows:
        states[state]+=1
        if state!='Pending Verification':continue
        reasons.update(json.loads(why))
        listing_state,_=verification(json.loads(payload),boundary)
        if source!=LIVE or listing_state!='Candidate':
            pending['listing_or_source_requires_refresh']+=1;continue
        eligible+=1
        cache=store.get_tax(identity,now.isoformat())
        if cache and cache[1]==LIVE and isinstance(cache[0],dict) and cache[0].get('id')==identity and not demo_marked(cache[0]):
            pending['valid_tax_cache_no_request']+=1
            if tax_verification(cache[0],now.date())[0]=='Pending Verification':pending['cached_tax_still_incomplete']+=1
        else:pending['tax_requests_needed_first_pass']+=1
        try:
            fresh=dt.timedelta(0)<=now-dt.datetime.fromisoformat(observed)<=dt.timedelta(hours=24)
        except (ValueError,TypeError):fresh=False
        if not fresh:pending['eligible_but_listing_stale']+=1
    latest=store.execute('SELECT stats FROM scan_jobs ORDER BY updated_at DESC LIMIT 1').fetchone()
    last_stats=json.loads(latest[0]) if latest else {}
    total=last_stats.get('total')
    return dict(listing_total_last_scan=total,listing_pages_last_scan=max(1,(int(total)+settings.page_size-1)//settings.page_size) if total is not None else None,total=len(rows),states=dict(states),pending=dict(pending),pending_reasons=dict(reasons),
        tax_eligible=eligible,requests_are_first_pass_not_guaranteed_qualifications=True,
        budget=store.budget_health(settings,now,record_warning=False),
        email_approved=bool(settings.email_enabled and settings.email_approval_id and os.getenv('RESEND_API_KEY')))


def main():
    settings=Settings.from_env()
    store=Store(validate_postgres_url(os.getenv('DATABASE_URL','')),initialize=False)
    try:
        # The production command cannot mutate data even by accident.
        store.execute('SET default_transaction_read_only = on')
        print(json.dumps(analyze(store,load_boundary(settings.boundary_file),settings),ensure_ascii=False))
    finally:store.close()

if __name__=='__main__':main()
