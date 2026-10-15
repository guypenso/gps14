"""Cloud scanner: inert by default; no execution occurs at import time."""
import argparse
import datetime as dt
import hashlib
import json
import logging
import os
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from .request import PARAMS, NoRedirect, diagnose
from .core import Boundary, basic_rejection, latest_tax
from .storage import Store, BudgetExceeded
from .schedule import scheduled_day
from .provenance import LiveRecord, LiveTax, demo_marked, require_live

LOG = logging.getLogger('rentalsourcing')


def event(name, **data):
    text = json.dumps({'time':dt.datetime.now(dt.timezone.utc).isoformat(), 'event':name, **data}, ensure_ascii=False)
    for key in ('RENTCAST_API_KEY','RESEND_API_KEY','DATABASE_URL'):
        value = os.getenv(key)
        if value: text = text.replace(value,'[REDACTED]')
    LOG.info(text)


class Client:
    def __init__(self, store):
        self.store = store
        self.count = 0
        self.opener = urllib.request.build_opener(NoRedirect)

    def get(self, path, params=None):
        if os.getenv('ENABLE_RENTCAST') != 'true': raise PermissionError('RentCast disabled')
        key = os.environ['RENTCAST_API_KEY']
        if self.count >= int(os.getenv('MAX_API_REQUESTS_PER_RUN','4')): raise BudgetExceeded('Run API budget reached')
        if path != '/listings/sale' and not path.startswith('/properties/'):
            raise ValueError('Unsupported endpoint')
        self.store.reserve(int(os.getenv('MAX_API_REQUESTS_PER_DAY','4')),int(os.getenv('MAX_API_REQUESTS_PER_MONTH','40')), total_limit=int(os.getenv('MAX_API_REQUESTS_TOTAL','40')))
        self.count += 1  # Attempts count before transport; no retries.
        if self.count > 1: time.sleep(1)
        url = 'https://api.rentcast.io/v1'+path
        if params: url += '?'+urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers={'X-Api-Key':key,'Accept':'application/json'})
        try:
            with self.opener.open(request,timeout=45) as response:
                data = json.load(response)
                total = response.headers.get('X-Total-Count')
                event('rentcast_ok',endpoint=path.split('/')[1],request_number=self.count)
                if isinstance(data,list):
                    data=[LiveRecord(r) if isinstance(r,dict) else r for r in data]
                elif isinstance(data,dict): data=LiveRecord(data)
                return data, total
        except Exception as exc:
            event('rentcast_failed',**diagnose(exc))
            raise


def send_email(row, tax):
    require_live(row,tax)
    if os.getenv('ENABLE_EMAIL') != 'true' or os.getenv('APP_ENV') != 'production':
        raise PermissionError('Production email is disabled')
    request = urllib.request.Request('https://api.resend.com/emails',method='POST',
        data=json.dumps({'from':os.environ['EMAIL_FROM'],'to':[os.environ['EMAIL_TO']],
             'subject':'RentalSourcing: '+row['formattedAddress'],
             'text':json.dumps({'property':row,'latestAnnualTax':tax,
                               'note':'Passed configured data filters; not investment advice.'},ensure_ascii=False)}).encode(),
        headers={'Authorization':'Bearer '+os.environ['RESEND_API_KEY'],'Content-Type':'application/json',
                 'Idempotency-Key':'rentalsourcing-'+hashlib.sha256(row['id'].encode()).hexdigest()})
    with urllib.request.build_opener(NoRedirect).open(request,timeout=30) as response:
        result = json.load(response)
        if not result.get('id'): raise RuntimeError('Email provider did not confirm acceptance')


def scan(store, boundary, client, email_enabled=False):
    stats = dict(received=0,total=None,basic=0,inside_city=0,tax_checked=0,qualified=0,sent=0,partial=False)
    max_pages = int(os.getenv('MAX_LISTING_PAGES','1'))
    limit = int(os.getenv('LISTING_LIMIT','20'))
    if not 1 <= limit <= 500 or max_pages < 1: raise ValueError('Invalid pagination settings')
    offset = 0
    for page in range(max_pages):
        try:
            rows,total = client.get('/listings/sale', dict(PARAMS,limit=limit,offset=offset))
        except BudgetExceeded:
            stats['partial']=True; event('budget_stop'); break
        if not isinstance(rows,list): raise ValueError('Listings response must be a list')
        if total is not None: stats['total']=int(total)
        stats['received'] += len(rows)
        for row in rows:
            if not isinstance(row,dict): continue
            if basic_rejection(row): continue
            stats['basic']+=1
            if not boundary.contains(row): continue
            stats['inside_city']+=1
            store.observe(row)
            if store.claimed(row['id']): continue
            try:
                record,_ = client.get('/properties/'+urllib.parse.quote(row['id'],safe=''))
            except BudgetExceeded:
                stats['partial']=True; event('budget_stop'); return stats
            stats['tax_checked']+=1
            if not isinstance(record,dict) or record.get('id') != row['id']:
                event('tax_identity_mismatch'); continue
            if demo_marked(record): continue
            tax = latest_tax(record)
            if tax is None: continue
            if isinstance(record,LiveRecord) and tax is not None: tax=LiveTax(tax)
            if email_enabled: require_live(row,tax)
            stats['qualified']+=1
            event('qualified',property_id=row['id'],tax_year=tax['year'],adjusted_tax=tax['adjusted'])
            if email_enabled and store.claim(row['id'],dict(listing=row,tax=tax)):
                try: send_email(row,tax)
                except Exception as exc:
                    store.mark(row['id'],'unknown')
                    event('email_delivery_unknown',error_type=type(exc).__name__)
                    raise
                store.mark(row['id'],'sent'); stats['sent']+=1
        offset += len(rows)
        if len(rows) < limit or (stats['total'] is not None and offset >= stats['total']): break
    else:
        stats['partial']=True
    return stats


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--check',action='store_true',help='Offline configuration check; no network')
    parser.add_argument('--scheduled',action='store_true',help='Only run at 08:00 America/New_York, once per local day')
    args=parser.parse_args()
    logging.basicConfig(level=logging.INFO,format='%(message)s')
    if args.check:
        event('offline_configuration',rentcast_enabled=os.getenv('ENABLE_RENTCAST')=='true',
              email_enabled=os.getenv('ENABLE_EMAIL')=='true',
              boundary_present=Path(os.getenv('CITY_BOUNDARY_FILE','data/pittsburgh.geojson')).is_file())
        return
    day = scheduled_day() if args.scheduled else None
    if args.scheduled and day is None:
        event('outside_pittsburgh_schedule'); return
    if os.getenv('ENABLE_RENTCAST') != 'true':
        event('disabled'); return
    for variable in ['RENTCAST_API_KEY','DATABASE_URL','CITY_BOUNDARY_FILE','CITY_BOUNDARY_SOURCE']:
        if not os.getenv(variable): raise ValueError('Missing configuration: '+variable)
    if not os.environ['DATABASE_URL'].startswith(('postgres://','postgresql://')):
        raise ValueError('Cloud execution requires persistent PostgreSQL')
    if os.getenv('ENABLE_EMAIL')=='true':
        if os.getenv('APP_ENV') != 'production': raise ValueError('Email requires APP_ENV=production')
        for variable in ['RESEND_API_KEY','EMAIL_FROM','EMAIL_TO']:
            if not os.getenv(variable): raise ValueError('Missing configuration: '+variable)
    boundary=Boundary(os.environ['CITY_BOUNDARY_FILE']) # Validate before any API request.
    store=Store(os.environ['DATABASE_URL'])
    run_id=str(uuid.uuid4())
    try:
        if not store.lock(): event('scan_already_running'); return
        if day and not store.claim_scheduled_day(day):
            event('scheduled_day_already_attempted'); return
        store.run(run_id,{'status':'running'})
        stats=scan(store,boundary,Client(store),os.getenv('ENABLE_EMAIL')=='true')
        store.run(run_id,{'status':'complete',**stats}); event('scan_complete',**stats)
    except Exception as exc:
        store.run(run_id,{'status':'failed','error_type':type(exc).__name__})
        raise
    finally: store.close()

if __name__=='__main__':
    try: main()
    except Exception as exc:
        event('scan_failed',**diagnose(exc))
        raise SystemExit(1)
