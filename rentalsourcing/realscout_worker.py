"""Bounded Receiving API retrieval and crash-recoverable PostgreSQL lease queue."""
import base64
import datetime as dt
import json
import re
import threading
import urllib.error
import urllib.request
from uuid import UUID,uuid4
from .request import NoRedirect
from .realscout_store import LinkVault,instant
from .realscout_inbound import process_received
from .realscout_auth import RawDkimVerifier,AuthFailure,ProviderAuthentication
from .telemetry import event


class RetrievalFailure(RuntimeError):
    def __init__(self,category,retryable=True):self.category=category;self.retryable=retryable


class ReceivingClient:
    def __init__(self,key,*,opener=None,limit=2_000_000,trusted_authserv_id=''):
        self.key=key;self.opener=opener or urllib.request.build_opener(NoRedirect);self.limit=limit
        self.trusted_authserv_id=trusted_authserv_id

    def get(self,email_id):
        identity=str(UUID(email_id))
        request=urllib.request.Request('https://api.resend.com/emails/receiving/'+identity,
            headers={'Authorization':'Bearer '+self.key,'Accept':'application/json','User-Agent':'RentalSourcing/1.0'})
        try:
            with self.opener.open(request,timeout=30) as response:
                if response.status!=200:raise RetrievalFailure('unexpected_receiving_status')
                raw=response.read(self.limit+1)
                if len(raw)>self.limit:raise RetrievalFailure('message_too_large',False)
                data=json.loads(raw)
        except urllib.error.HTTPError as exc:
            code=exc.code;exc.close()
            raise RetrievalFailure('receiving_http_'+str(code),code in {404,408,429,500,502,503,504}) from None
        except (urllib.error.URLError,TimeoutError,OSError):raise RetrievalFailure('receiving_network_failure') from None
        except (ValueError,UnicodeError):raise RetrievalFailure('invalid_receiving_json',False) from None
        if not isinstance(data,dict) or data.get('id')!=identity:raise RetrievalFailure('receiving_identity_mismatch',False)
        # Supported HTML data URI representation: decode locally, never follow URLs.
        body=data.get('html')
        if isinstance(body,str) and body.startswith('data:'):
            try:
                prefix,encoded=body.split(',',1)
                if prefix.lower() not in {'data:text/html;base64','data:text/html;charset=utf-8;base64'}:raise ValueError
                decoded=base64.b64decode(encoded,validate=True)
                if len(decoded)>self.limit:raise ValueError
                data['html']=decoded.decode('utf-8')
            except (ValueError,UnicodeError):raise RetrievalFailure('unsupported_html_encoding',False) from None
        # Always normalize at the trusted transport boundary, never trust an
        # undocumented/sender-supplied top-level authentication object.
        auth=data.pop('authentication',None)
        data.pop('_receiver_authentication',None)
        allowed={'pass','fail','gray','processing_failed','unknown'}
        if isinstance(auth,dict) and all(auth.get(k) in allowed for k in ('spf','dkim','dmarc')):
            data['_receiver_authentication']=ProviderAuthentication(identity,auth['spf'],auth['dkim'],auth['dmarc'])
        data['authentication']={}  # Untrusted caller dictionaries still never cross process_received.
        return data


def health(store,name,payload,now):
    store.execute('INSERT INTO realscout_health VALUES (?,?,?) ON CONFLICT(name) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at',
                  (name,json.dumps(payload),now.isoformat()))


def claim(store,settings,now):
    token=str(uuid4());until=(now+dt.timedelta(seconds=settings.lease_seconds)).isoformat()
    with store.transaction():
        sql="""SELECT email_id,recipient_alias FROM realscout_inbound_jobs
          WHERE ((state IN ('pending','retry') AND next_attempt_at<=?) OR
                 (state='processing' AND lease_until<=?)) AND attempts<?
          ORDER BY received_at,email_id LIMIT 1"""
        if store.pg:sql+=' FOR UPDATE SKIP LOCKED'
        row=store.execute(sql,(now.isoformat(),now.isoformat(),settings.max_attempts)).fetchone()
        if not row:return None
        store.execute("UPDATE realscout_inbound_jobs SET state='processing',attempts=attempts+1,lease_token=?,lease_until=?,updated_at=? WHERE email_id=?",
                      (token,until,now.isoformat(),row[0]))
    return row[0],row[1],token


def run_once(store,settings,*,client=None,clock=None,verifier=None):
    if not settings.ingest_enabled:raise PermissionError('RealScout private retrieval is disabled')
    clock=clock or (lambda:dt.datetime.now(dt.timezone.utc))
    client=client or ReceivingClient(settings.receiving_key,limit=settings.message_limit,
        trusted_authserv_id=settings.trusted_authserv_id if settings.authentication_contract_approval else '')
    verifier=verifier or RawDkimVerifier(store,settings.raw_download_hosts,clock=clock)
    vault=LinkVault(settings.link_key);counts={'processed':0,'retried':0,'quarantined':0}
    # Exhausted interrupted jobs must not remain stuck in processing forever.
    store.execute("UPDATE realscout_inbound_jobs SET state='quarantined',error_category='attempts_exhausted',lease_token=NULL,lease_until=NULL WHERE attempts>=? AND state IN ('processing','pending','retry') AND (lease_until IS NULL OR lease_until<=?)",
                  (settings.max_attempts,clock().isoformat()))
    for _ in range(settings.max_jobs):
        item=claim(store,settings,clock())
        if not item:break
        email_id,alias,token=item
        try:
            policy=next((p for p in settings.policies if p.inbox.lower()==alias),None)
            if policy is None:raise RetrievalFailure('inbox_profile_no_longer_authorized',False)
            accepted=store.execute('SELECT 1 FROM realscout_auth_evidence a JOIN realscout_messages m ON m.id=a.message_key WHERE a.email_id=?',(email_id,)).fetchone()
            if accepted:
                # Prior verified ingest committed before a crash/ACK failure: no private re-fetch.
                store.execute("UPDATE realscout_inbound_jobs SET state='processed',error_category=NULL,lease_token=NULL,lease_until=NULL,updated_at=? WHERE email_id=? AND lease_token=?",
                    (clock().isoformat(),email_id,token))
                counts['processed']+=1
                continue
            data=client.get(email_id)
            if data.get('id')!=email_id:raise RetrievalFailure('receiving_identity_mismatch',False)
            owned=store.execute('SELECT lease_token,lease_until FROM realscout_inbound_jobs WHERE email_id=?',(email_id,)).fetchone()
            if owned[0]!=token or instant(owned[1])<=clock():raise RetrievalFailure('worker_lease_lost')
            proof=verifier.verify(data,policy)
            owned=store.execute('SELECT lease_token,lease_until FROM realscout_inbound_jobs WHERE email_id=?',(email_id,)).fetchone()
            if owned[0]!=token or instant(owned[1])<=clock():raise RetrievalFailure('worker_lease_lost')
            result=process_received(store,data,policy,authorized=True,vault=vault,processed_at=clock().isoformat(),verification=proof)
            store.execute("UPDATE realscout_inbound_jobs SET state='processed',error_category=NULL,lease_token=NULL,lease_until=NULL,updated_at=? WHERE email_id=? AND lease_token=?",
                          (clock().isoformat(),email_id,token))
            counts['processed']+=1
        except Exception as exc:
            if isinstance(exc,(RetrievalFailure,AuthFailure)):category,retry=exc.category,exc.retryable
            elif isinstance(exc,PermissionError):category,retry='sender_or_destination_authentication_failed',False
            elif isinstance(exc,(ValueError,KeyError,TypeError,UnicodeError)):category,retry='template_or_metadata_review_required',False
            else:category,retry='internal_processing_failure',True
            attempt=store.execute('SELECT attempts FROM realscout_inbound_jobs WHERE email_id=?',(email_id,)).fetchone()[0]
            retry=retry and attempt<settings.max_attempts
            state='retry' if retry else 'quarantined'
            due=clock()+dt.timedelta(seconds=min(3600,30*2**(attempt-1)))
            store.execute('UPDATE realscout_inbound_jobs SET state=?,error_category=?,next_attempt_at=?,lease_token=NULL,lease_until=NULL,updated_at=? WHERE email_id=? AND lease_token=?',
                          (state,category,due.isoformat(),clock().isoformat(),email_id,token))
            counts['retried' if retry else 'quarantined']+=1
            event('realscout_job_result',state=state,category=category)  # No email IDs, body, links or sender.
    health(store,'worker',counts,clock())
    return counts


def monitor(store,now):
    queue={r[0]:r[1] for r in store.execute('SELECT state,COUNT(*) FROM realscout_inbound_jobs GROUP BY state').fetchall()}
    beats={r[0]:dict(updated_at=r[2],payload=json.loads(r[1])) for r in store.execute('SELECT name,payload,updated_at FROM realscout_health').fetchall()}
    oldest=store.execute("SELECT MIN(received_at) FROM realscout_inbound_jobs WHERE state IN ('pending','retry','processing')").fetchone()[0]
    return dict(queue=queue,heartbeats=beats,oldest_unprocessed_at=oldest,
                backlog_delayed=bool(oldest and now-instant(oldest)>dt.timedelta(hours=1)),
                worker_stale=not beats.get('worker') or now-instant(beats['worker']['updated_at'])>dt.timedelta(minutes=5))


def start_background(store_factory,settings):
    stop=threading.Event()
    def loop():
        while not stop.is_set():
            store=None
            try:
                store=store_factory();run_once(store,settings)
            except Exception:event('realscout_worker_unavailable',category='worker_iteration_failed')
            finally:
                if store:store.close()
            stop.wait(settings.poll_seconds)
    thread=threading.Thread(target=loop,name='realscout-inbound-worker',daemon=True);thread.start()
    return stop,thread


def requeue(store,email_id,now):
    """Explicit operator recovery after fixing configuration/template; never alters API counters."""
    identity=str(UUID(email_id))
    with store.transaction():
        changed=store.execute("UPDATE realscout_inbound_jobs SET state='pending',attempts=0,error_category=NULL,next_attempt_at=?,lease_until=NULL,lease_token=NULL,updated_at=? WHERE email_id=? AND state IN ('quarantined','retry') RETURNING email_id",
                              (now.isoformat(),now.isoformat(),identity)).fetchone()
        if not changed:raise ValueError('Only quarantined or retry jobs may be explicitly requeued')
