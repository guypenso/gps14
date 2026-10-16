"""Real RentCast HTTPS collector with persistent caching and approval-bound quotas."""
import datetime as dt
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from .config import Settings
from .request import NoRedirect, diagnose
from .storage import BudgetExceeded
from .provenance import LiveRecord
from .telemetry import event

LIVE='rentcast-live'

class RequestDeferred(BudgetExceeded):pass

def tax_expiry(record,now,settings):
    from .core import tax_verification
    hours=settings.tax_cache_hours if tax_verification(record,now.date())[0]!='Pending Verification' else settings.negative_tax_cache_hours
    return min(now+dt.timedelta(hours=hours),dt.datetime(now.year+1,1,1,tzinfo=dt.timezone.utc))

class Client:
    source=LIVE
    def __init__(self,store,settings=None,clock=None,opener=None,sleep=time.sleep):
        self.store=store;self.settings=settings or Settings.from_env()
        self.clock=clock or (lambda:dt.datetime.now(dt.timezone.utc))
        self.opener=opener or urllib.request.build_opener(NoRedirect)
        self.sleep=sleep;self.count=0;self.cache_hits=0;self.last_fetched_at=None;self.last_error=None

    def get(self,path,params=None,cache=True):
        # A session lock covers cache lookup, reservation and response persistence.
        # A second worker rechecks the cache after the first worker finishes.
        raw=path+'?'+urllib.parse.urlencode(sorted((params or {}).items()))
        lock=int.from_bytes(hashlib.sha256(raw.encode()).digest()[:8],'big',signed=True)
        if self.store.pg:self.store.execute('SELECT pg_advisory_lock(?)',(lock,))
        try:return self._get(path,params,cache)
        finally:
            if self.store.pg:self.store.execute('SELECT pg_advisory_unlock(?)',(lock,))

    def _get(self,path,params=None,cache=True):
        if path!='/listings/sale' and not path.startswith('/properties/'):
            raise ValueError('Unsupported RentCast endpoint')
        query=urllib.parse.urlencode(sorted((params or {}).items()))
        url='https://api.rentcast.io/v1'+path+('?' + query if query else '')
        key=hashlib.sha256(url.encode()).hexdigest();now=self.clock()
        if cache:
            saved=self.store.cache_get(key,now.isoformat())
            if saved and saved[1]==LIVE:
                body=saved[0];self.cache_hits+=1;self.last_fetched_at=body['fetched_at']
                return self.wrap(body['data']),body['total']
        deferred=self.store.execute('SELECT retry_after FROM api_attempts WHERE request_key=?',(key,)).fetchone()
        if deferred and deferred[0]>now.isoformat():raise RequestDeferred('Identical API request deferred after an interrupted or failed attempt')
        if not self.settings.enabled:raise PermissionError('Live RentCast collection disabled')
        if not os.getenv('RENTCAST_API_KEY'):raise ValueError('RENTCAST_API_KEY missing')
        request=urllib.request.Request(url,headers={'X-Api-Key':os.environ['RENTCAST_API_KEY'],'Accept':'application/json'})
        for attempt in range(self.settings.retries+1):
            if self.count>=self.settings.per_run:raise BudgetExceeded('Run API allowance reached')
            health=self.store.reserve_api(self.settings,self.clock(),tax_request=path.startswith('/properties/'))
            if health['warning']:event('budget_warning',**health)
            self.count+=1
            if self.count>1:self.sleep(1)
            # Commit before the wire: after a crash, an uncertain billed GET is
            # not repeated immediately. Explicit retries remain quota-bound.
            self.store.execute('INSERT INTO api_attempts VALUES (?,?,?,?) ON CONFLICT(request_key) DO UPDATE SET state=excluded.state,attempted_at=excluded.attempted_at,retry_after=excluded.retry_after',
                (key,'in_flight',self.clock().isoformat(),(self.clock()+dt.timedelta(minutes=15)).isoformat()))
            try:
                with self.opener.open(request,timeout=self.settings.timeout) as response:
                    if response.status!=200:raise ValueError('Unexpected HTTP status')
                    data=json.load(response);total=response.headers.get('X-Total-Count')
                    if path=='/listings/sale' and not isinstance(data,list):raise ValueError('Listings must be a list')
                    if path.startswith('/properties/') and not isinstance(data,dict):raise ValueError('Property must be an object')
                self.last_fetched_at=self.clock().isoformat()
                if cache:
                    expires=self.clock()+dt.timedelta(minutes=self.settings.listing_cache_minutes) if path=='/listings/sale' else tax_expiry(data,self.clock(),self.settings)
                    self.store.cache_put(key,dict(data=data,total=total,fetched_at=self.last_fetched_at),LIVE,expires.isoformat())
                self.store.execute('DELETE FROM api_attempts WHERE request_key=?',(key,))
                event('rentcast_success',endpoint=path.split('/')[1],attempts=self.count)
                return self.wrap(data),total
            except Exception as exc:
                self.last_error=diagnose(exc)
                if isinstance(exc,urllib.error.HTTPError):
                    # Do not log raw bodies: an upstream proxy can echo credentials.
                    try:
                        body=exc.read(4096).decode('utf-8',errors='replace').lower()
                        self.last_error['body_category']=('billing' if 'billing/subscription' in body else
                            'proxy_policy' if any(word in body for word in ('egress','proxy policy','destination is not allowed')) else
                            'authentication' if any(word in body for word in ('invalid api key','unauthorized api key')) else 'unclassified')
                    except Exception:self.last_error['body_category']='unavailable'
                    finally:exc.close()
                self.store.operation('provider-error:'+self.clock().isoformat(),'RentCast request failed: '+str(self.last_error.get('http_status') or self.last_error['stage'])+'. No credentials recorded.')
                event('rentcast_failure',**self.last_error)
                retryable=(isinstance(exc,urllib.error.HTTPError) and exc.code in {429,500,502,503,504}) or \
                    (isinstance(exc,(urllib.error.URLError,TimeoutError)) and not isinstance(exc,urllib.error.HTTPError))
                if isinstance(exc,urllib.error.HTTPError) and exc.code==404 and path.startswith('/properties/'):
                    self.store.execute('UPDATE api_attempts SET retry_after=? WHERE request_key=?',((self.clock()+dt.timedelta(hours=self.settings.negative_tax_cache_hours)).isoformat(),key))
                if not retryable or attempt==self.settings.retries:raise
                self.sleep(min(2**(attempt+1),8))

    @staticmethod
    def wrap(data):
        if isinstance(data,list):return [LiveRecord(r) if isinstance(r,dict) else r for r in data]
        return LiveRecord(data) if isinstance(data,dict) else data
