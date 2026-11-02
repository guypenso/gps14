"""Zero-provider-call evidence recovery and factual review order. No yield estimates."""
import datetime as dt
import hashlib
import json
import urllib.parse
from .core import verification,tax_verification,number,historical_reduction
from .collector import LIVE,tax_expiry
from .provenance import demo_marked

RESEARCH='Tax research: https://realestate.alleghenycounty.us/search — verify latest annual county, municipal and school bills; assessed values are not tax bills.'


def review_deferrals():
    """Private snapshot of confirmed permanent PENTEN rejections; paid-work only."""
    import os,re
    try:
        values=json.loads(os.getenv('TAX_REVIEW_DEFERRED_SOURCE_HASHES','[]'))
        if not isinstance(values,list) or len(values)>20000 or any(not isinstance(v,str) or not re.fullmatch(r'[a-f0-9]{64}',v) for v in values):raise ValueError
        return frozenset(values)
    except (ValueError,TypeError):raise ValueError('Invalid private tax-review deferral configuration') from None


def review_deferred(identity,store=None):
    if hashlib.sha256(str(identity).encode()).hexdigest() in review_deferrals():return True
    if store:
        row=store.execute('SELECT payload FROM settings WHERE name=?',('penten_review:'+str(identity),)).fetchone()
        if row and json.loads(row[0]).get('status')=='rejected':return True
    return False


def recent(value,now):
    try:
        when=dt.datetime.fromisoformat(value.replace('Z','+00:00'))
        return when.tzinfo is not None and dt.timedelta(0)<=now-when<=dt.timedelta(hours=24)
    except (TypeError,ValueError,AttributeError):return False


def reconcile(store,settings,now):
    """Recover interrupted response-to-history writes, preserving original provenance/TTL."""
    copied=resolved=invalid=0
    for identity,source in store.execute('SELECT id,source FROM properties').fetchall():
        if source!=LIVE or store.get_tax(identity,now.isoformat()):continue
        url='https://api.rentcast.io/v1/properties/'+urllib.parse.quote(identity,safe='')
        key=hashlib.sha256(url.encode()).hexdigest()
        saved=store.execute('SELECT payload,source,expires_at FROM response_cache WHERE key=? AND expires_at>?',(key,now.isoformat())).fetchone()
        if not saved or saved[1]!=LIVE:continue
        try:
            body=json.loads(saved[0]);record=body['data'];fetched=dt.datetime.fromisoformat(body['fetched_at'].replace('Z','+00:00'))
            if fetched.tzinfo is None or fetched>now or not isinstance(record,dict) or record.get('id')!=identity or demo_marked(record):raise ValueError('Untrusted cache')
            expires=min(dt.datetime.fromisoformat(saved[2].replace('Z','+00:00')),tax_expiry(record,fetched,settings))
            if expires<=now:continue
            # Do not replace a newer record, including newer conflicting evidence.
            old=store.execute('SELECT fetched_at FROM tax_history WHERE property_id=?',(identity,)).fetchone()
            if old and dt.datetime.fromisoformat(old[0].replace('Z','+00:00'))>fetched:continue
        except (ValueError,TypeError,KeyError,AttributeError):invalid+=1;continue
        store.save_tax(identity,record,LIVE,fetched.isoformat(),expires.isoformat());copied+=1
        if tax_verification(record,now.date())[0]!='Pending Verification':resolved+=1
    return dict(recovered_cache_records=copied,resolved_tax_decisions=resolved,invalid_cache_records=invalid)


def previous_price(store,identity,current):
    row=store.execute('SELECT price,previous_price,reduced FROM price_history WHERE property_id=? ORDER BY observed_at DESC,id DESC LIMIT 1',(identity,)).fetchone()
    return row[1] if row and row[2] and row[0]==current and number(row[1]) and number(current) and row[1]>current else None


def review_key(row,boundary,observed,now,previous=None):
    """Lexicographic review order, with no qualification threshold or return score."""
    complete=verification(row,boundary)[0]=='Candidate'
    fresh=recent(observed,now)
    previous=previous or historical_reduction(row)
    reduction=(previous-row['price'])/previous if number(previous) and previous>0 and number(row.get('price')) and previous>row['price'] else 0
    def value(key,default):return row[key] if number(row.get(key)) else default
    return (not complete,not fresh,-reduction,value('price',float('inf')),
            -value('bedrooms',-1),-value('bathrooms',-1),-value('squareFootage',-1),
            # Date only breaks ties; invalid/unknown timestamps rank last.
            -dt.datetime.fromisoformat(observed.replace('Z','+00:00')).timestamp() if fresh else 0,str(row.get('id','')))


def priorities(store,boundary,now):
    records=[]
    for identity,raw,state,source,observed in store.execute('SELECT id,payload,state,source,observed_at FROM properties').fetchall():
        row=json.loads(raw)
        if source!=LIVE or verification(row,boundary)[0]!='Candidate' or review_deferred(identity,store):continue
        cached=store.get_tax(identity,now.isoformat())
        valid=cached and cached[1]==LIVE and isinstance(cached[0],dict) and cached[0].get('id')==identity and not demo_marked(cached[0])
        if valid and tax_verification(cached[0],now.date())[0]!='Pending Verification':continue
        prior=previous_price(store,identity,row.get('price'))
        records.append(dict(source_id=identity,address=row.get('formattedAddress'),price=row.get('price'),bedrooms=row.get('bedrooms'),bathrooms=row.get('bathrooms'),square_feet=row.get('squareFootage'),previous_price=prior,observed_at=observed,fresh=recent(observed,now),tax_status='pending_verification',key=review_key(row,boundary,observed,now,prior)))
    records.sort(key=lambda r:r['key'])
    return records


def main():
    import argparse,os
    from .storage import Store
    from .database import validate_postgres_url
    from .geography import load_boundary
    from .config import Settings
    parser=argparse.ArgumentParser(description='Read-only private backlog report; no provider calls or emails')
    parser.add_argument('--report',action='store_true',required=True);parser.parse_args()
    store=Store(validate_postgres_url(os.getenv('DATABASE_URL','')),initialize=False)
    try:
        store.execute('BEGIN READ ONLY')
        now=dt.datetime.now(dt.timezone.utc);boundary=load_boundary(Settings.from_env().boundary_file)
        from .diagnostics import funnel
        report=funnel(store,boundary,now)
        report['top_pending']=[{k:v for k,v in p.items() if k!='key'} for p in priorities(store,boundary,now)[:10]]
        print(json.dumps(report,ensure_ascii=False,indent=2))
    finally:
        store.execute('ROLLBACK');store.close()

if __name__=='__main__':main()
