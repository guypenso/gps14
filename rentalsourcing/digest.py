"""Durable daily digest with Resend idempotency and safe recovery of uncertain sends."""
import datetime as dt
import hashlib
import html
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from .collector import LIVE
from .core import demo_marked,verification,tax_verification,historical_reduction,number
from .request import NoRedirect
from .telemetry import event

class ReconciliationRequired(RuntimeError):pass


def money(value):return '${:,.2f}'.format(value)


def recent_timestamp(value,now,hours=24):
    try:
        then=dt.datetime.fromisoformat(value.replace('Z','+00:00'))
        return then.tzinfo is not None and dt.timedelta(0)<=now-then<dt.timedelta(hours=hours)
    except (AttributeError,TypeError,ValueError):return False


def listing_link(row):
    value=row.get('listingUrl')
    if not isinstance(value,str):return None
    try:
        parsed=urllib.parse.urlsplit(value)
        if parsed.scheme=='https' and parsed.hostname and not parsed.username and not parsed.password:
            return value
    except ValueError:pass
    return None


def render(day,items,stats,health,operations):
    failed=bool(stats.get('error_type'))
    incomplete=failed or stats.get('partial')
    status='נכשלה / נדרשת בדיקה' if failed else ('חלקית / נדרשת השלמה' if incomplete else 'הושלמה')
    lines=[f'RentalSourcing — {day}',('מבוסס על נתונים שמורים בלבד; לא בוצעה סריקה חדשה.' if stats.get('saved_data_only') else f'מצב הסריקה: {status}')]
    items=sorted(items,key=lambda item:item['kind']!='price_reduction')
    cards=[]
    for item in items:
        p=item['listing'];t=item['tax'];previous=item.get('previous_price')
        label='הורדת מחיר חדשה' if item['kind']=='price_reduction' else 'נכס חדש שאומת'
        if stats.get('saved_data_only'):label='הורדת מחיר מתועדת' if item['kind']=='price_reduction' else 'נכס מאומת מנתונים שמורים'
        map_url='https://www.google.com/maps/search/?'+urllib.parse.urlencode({'api':1,'query':p['formattedAddress']})
        text=f"{label}: {p['formattedAddress']}\nמחיר: {money(p['price'])}"
        if previous is not None and previous>p['price']:
            text+=f" | קודם: {money(previous)} | ירידה: {money(previous-p['price'])}"
        text+=f"\nחדרי שינה: {p['bedrooms']} | אמבטיות: {p['bathrooms']} | שטח: {p['squareFootage']} sqft | ZIP: {p['zipCode']}"
        text+=f"\nמס שנתי לשנת {t['year']}: {money(t['annual'])} + $100 = {money(t['adjusted'])}"
        text+=f"\nמקור: RentCast Sale Listings | מזהה: {p['id']}\nאימות: {item['verified_at']} | נתוני מס נשלפו: {item['tax_fetched_at']}\nמפה: {map_url}"
        if item.get('listing_observed_at'):text+='\nהמודעה נצפתה לאחרונה: '+item['listing_observed_at']
        link=listing_link(p)
        text+='\nמודעה: '+(link or 'קישור מודעה לא הוחזר על ידי הספק')
        lines.append(text)
        cards.append('<section style="border:1px solid #ddd;padding:16px;margin:12px 0"><pre style="white-space:pre-wrap;font:inherit">'+html.escape(text)+'</pre></section>')
    if not items:
        lines.append('לא ניתן לקבוע שאין עסקאות חדשות: הסריקה נכשלה או לא הושלמה.' if incomplete else 'אין נכסים חדשים או הורדות מחיר חדשות שעברו את כל בדיקות האימות.')
    if failed:lines.append('סוג התקלה: '+str(stats['error_type']))
    footer=f"ניסיונות API שנותרו באישור ובמחזור: {health.get('remaining',0)}. נכסים שחסר להם מידע נשארים Pending Verification. שנת המס היא האחרונה הזמינה מהספק."
    lines.append(footer)
    for warning in operations:lines.append('הודעת מערכת: '+warning)
    markup='<html lang="he" dir="rtl"><head><meta name="viewport" content="width=device-width,initial-scale=1"></head><body style="font-family:Arial,sans-serif;max-width:680px;margin:auto;padding:16px"><h1>RentalSourcing</h1><p>'+html.escape(lines[1])+'</p>'+''.join(cards)
    if not items:markup+='<p>'+html.escape(lines[2])+'</p>'
    if failed:markup+='<p>'+html.escape('סוג התקלה: '+str(stats['error_type']))+'</p>'
    markup+='<p>'+html.escape(footer)+'</p>'+''.join('<p>'+html.escape(w)+'</p>' for w in operations)+'</body></html>'
    return '\n\n'.join(lines),markup


class Digest:
    def __init__(self,store,boundary,settings,clock=None,opener=None,realscout_settings=None,discovery_settings=None):
        self.store=store;self.boundary=boundary;self.settings=settings;self.realscout_settings=realscout_settings;self.discovery_settings=discovery_settings
        self.clock=clock or (lambda:dt.datetime.now(dt.timezone.utc))
        self.opener=opener or urllib.request.build_opener(NoRedirect)

    def prepare(self,job_id,day,stats):
        if not self.settings.email_enabled or not self.settings.email_approval_id or not os.getenv('RESEND_API_KEY'):
            return None
        digest_day='daily:'+day
        old=self.store.execute('SELECT id FROM outbox WHERE job_id=?',(digest_day,)).fetchone()
        if old:return old[0]
        items=[];ids=[]
        rows=self.store.execute("SELECT e.id,e.property_id,e.kind,e.previous_price,e.payload,e.price,e.revision FROM deal_events e JOIN scan_tasks t ON e.id=t.event_id WHERE t.job_id=? AND e.state='ready' ORDER BY t.priority,e.created_at,e.id",(job_id,)).fetchall()
        for identity,property_id,kind,previous,payload,price,revision in rows:
            data=json.loads(payload);p=self.store.get_property(property_id)
            if not data or not p or p['state']!='Qualified' or p['source']!=LIVE or data.get('source')!=LIVE or data.get('tax_source')!=LIVE:continue
            row=data.get('listing',{})
            if demo_marked(row) or p['revision']!=revision or p['payload'].get('price')!=price:continue
            if verification(row,self.boundary)[0]!='Candidate':continue
            now=self.clock()
            if not recent_timestamp(data.get('verified_at'),now):continue
            if not recent_timestamp(p['observed_at'],now):continue
            cached=self.store.get_tax(property_id,now.isoformat())
            if not cached or cached[1]!=LIVE or cached[0].get('id')!=property_id or demo_marked(cached[0]):continue
            state,tax,_=tax_verification(cached[0],now.date())
            if state!='Qualified' or tax!=data.get('tax'):continue
            items.append(dict(data,kind=kind,previous_price=previous));ids.append(identity)
        health=self.store.budget_health(self.settings,self.clock())
        text,markup=render(day,items,stats,health,self.store.operations())
        status='תקלה בסריקה' if stats.get('error_type') else ('סריקה חלקית' if stats.get('partial') else f'{len(items)} נכסים מאומתים')
        body=dict(from_=self.settings.email_from,to=[self.settings.email_to],
                  subject=f'RentalSourcing | {day} | {status}',text=text,html=markup)
        body['from']=body.pop('from_')
        outbox_id='daily-'+hashlib.sha256(digest_day.encode()).hexdigest()
        with self.store.transaction():
            self.store.execute('INSERT INTO outbox VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(job_id) DO NOTHING',
                (outbox_id,digest_day,'pending',json.dumps(body),json.dumps(ids),None,None,self.clock().isoformat()))
            for identity in ids:self.store.execute("UPDATE deal_events SET state='queued' WHERE id=? AND state='ready'",(identity,))
        return outbox_id

    def prepare_stored(self,day):
        """Bootstrap unsent approved records; no scanner or API client is invoked."""
        from .criteria import reevaluate
        now=self.clock();reevaluate(self.store,self.boundary,now)
        digest_day='daily:'+day
        old=self.store.execute('SELECT id,state,first_attempt_at FROM outbox WHERE job_id=?',(digest_day,)).fetchone()
        if old and (old[2] or old[1]=='sent'):return old[0]
        items=[];ids=[]
        with self.store.transaction():
            for identity,payload,revision,observed in self.store.execute("SELECT id,payload,revision,observed_at FROM properties WHERE state='Qualified' AND source=? ORDER BY id",(LIVE,)).fetchall():
                row=json.loads(payload);cached=self.store.get_tax(identity,now.isoformat())
                if not cached or cached[1]!=LIVE or cached[0].get('id')!=identity or demo_marked(cached[0]):continue
                if verification(row,self.boundary)[0]!='Candidate' or not recent_timestamp(observed,now):continue
                state,tax,_=tax_verification(cached[0],now.date())
                if state!='Qualified':continue
                event_id=f'{identity}:price:{revision}'
                existing=self.store.execute('SELECT state FROM deal_events WHERE id=?',(event_id,)).fetchone()
                if existing and existing[0] in {'sent','superseded'}:continue
                price_row=self.store.execute('SELECT previous_price,reduced FROM price_history WHERE property_id=? ORDER BY observed_at DESC,id DESC LIMIT 1',(identity,)).fetchone()
                previous=price_row[0] if price_row and price_row[1] else historical_reduction(row)
                reduced=number(previous) and previous>row['price']
                kind='price_reduction' if reduced else 'new'
                data=dict(listing=row,tax=tax,source=LIVE,tax_source=LIVE,verified_at=now.isoformat(),tax_fetched_at=cached[2],listing_observed_at=observed)
                self.store.execute('INSERT INTO deal_events VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING',
                    (event_id,identity,kind,row['price'],previous,revision,'ready',json.dumps(data),now.isoformat()))
                items.append(dict(data,kind=kind,previous_price=previous));ids.append(event_id)
            text,markup=render(day,items,{'saved_data_only':True},self.store.budget_health(self.settings,now),self.store.operations())
            body={'from':self.settings.email_from,'to':[self.settings.email_to],
                'subject':f'RentalSourcing | {day} | '+(f'{len(items)} נכסים מאומתים מנתונים שמורים' if items else 'עדכון נתונים שמורים — אין הזדמנויות חדשות מאומתות'),
                'text':text,'html':markup}
            outbox_id='daily-'+hashlib.sha256(digest_day.encode()).hexdigest()
            if old:
                previous_ids=json.loads(self.store.execute('SELECT event_ids FROM outbox WHERE id=?',(old[0],)).fetchone()[0])
                for prior in previous_ids:self.store.execute("UPDATE deal_events SET state='ready' WHERE id=? AND state='queued'",(prior,))
            self.store.execute("INSERT INTO outbox VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET state='pending',payload=excluded.payload,event_ids=excluded.event_ids,updated_at=excluded.updated_at WHERE outbox.first_attempt_at IS NULL AND outbox.state IN ('pending','cancelled')",
                (outbox_id,digest_day,'pending',json.dumps(body),json.dumps(ids),None,None,now.isoformat()))
            for identity in ids:self.store.execute("UPDATE deal_events SET state='queued' WHERE id=? AND state='ready'",(identity,))
        return outbox_id

    def prepare_test(self):
        if not self.settings.email_from or not self.settings.email_to:
            raise ValueError('Configure EMAIL_FROM and EMAIL_TO privately before preparing test')
        token=hashlib.sha256(json.dumps([self.settings.email_approval_id,self.settings.email_from,self.settings.email_to]).encode()).hexdigest()
        identity='test-'+token
        text='RentalSourcing — בדיקת מסירה מאושרת. לא בוצעה סריקת RentCast. מייל זה אינו התראת נכס ואינו מסמן הזדמנויות כנשלחות.'
        body={'from':self.settings.email_from,'to':[self.settings.email_to],'subject':'RentalSourcing — בדיקת מסירה','text':text}
        self.store.execute('INSERT INTO outbox VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(job_id) DO NOTHING',
            (identity,'test:'+token,'pending',json.dumps(body),'[]',None,None,self.clock().isoformat()))
        return identity

    def inspect_request(self,identity):
        """Inspect the persisted request without a network call or secret output."""
        from .resend_api import request as resend_request
        row=self.store.execute('SELECT state,payload,first_attempt_at,provider_id FROM outbox WHERE id=?',(identity,)).fetchone()
        if not row:raise ValueError('Unknown outbox message')
        state,body,first,provider=row;envelope=json.loads(body)
        req=resend_request(body,'[REDACTED]',identity)
        evidence=self.store.execute('SELECT payload FROM email_errors WHERE outbox_id=?',(identity,)).fetchone()
        from email.utils import parseaddr
        sender=parseaddr(envelope.get('from',''))[1]
        return dict(outbox_id=identity,state=state,attempted=bool(first),provider_id_present=bool(provider),
            endpoint=req.full_url,method=req.get_method(),headers=dict(req.header_items()),
            payload_fields=sorted(envelope),recipient_count=len(envelope.get('to',[])),
            onboarding_sender=sender.lower()=='onboarding@resend.dev',
            envelope_matches_config=envelope.get('from')==self.settings.email_from and envelope.get('to')==[self.settings.email_to],
            last_error=json.loads(evidence[0]) if evidence else None)

    def replace_rejected_test(self,identity):
        """Prepare only; retain the failed record and never rearm uncertain sends."""
        if not identity.startswith('test-'):raise ValueError('Only delivery-test messages may be replaced')
        row=self.store.execute('SELECT state,payload,event_ids,provider_id,updated_at FROM outbox WHERE id=?',(identity,)).fetchone()
        evidence=self.store.execute('SELECT payload FROM email_errors WHERE outbox_id=?',(identity,)).fetchone()
        if (not row or row[0]!='failed' or row[3] or json.loads(row[2]) or not evidence
                or not json.loads(evidence[0]).get('confirmed_rejection')):
            raise ReconciliationRequired('No confirmed rejection evidence; inspect Resend request logs before replacing this message')
        body=json.loads(row[1])
        if body.get('from')!=self.settings.email_from or body.get('to')!=[self.settings.email_to]:
            raise PermissionError('Replacement must keep the approved frozen sender and recipient')
        token=hashlib.sha256((identity+':'+row[4]).encode()).hexdigest()
        replacement='test-retry-'+token
        self.store.execute('INSERT INTO outbox VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(job_id) DO NOTHING',
            (replacement,'test-retry:'+token,'pending',row[1],'[]',None,None,self.clock().isoformat()))
        self.store.operation('email-replacement:'+identity,'Confirmed rejected test replaced by '+replacement+'; preparation only, no email sent.')
        return replacement

    def delivery_status(self,identity):
        row=self.store.execute('SELECT provider_id,state FROM outbox WHERE id=?',(identity,)).fetchone()
        if not row:raise ValueError('Unknown outbox message')
        if not row[0]:return dict(outbox_id=identity,state=row[1],delivered=False)
        if not os.getenv('RESEND_API_KEY'):raise ValueError('RESEND_API_KEY missing')
        request=urllib.request.Request('https://api.resend.com/emails/'+urllib.parse.quote(row[0],safe=''),headers={'Authorization':'Bearer '+os.environ['RESEND_API_KEY']})
        with self.opener.open(request,timeout=self.settings.timeout) as response:
            data=json.load(response)
            if response.status!=200 or data.get('id')!=row[0]:raise RuntimeError('Resend delivery lookup not confirmed')
        return dict(outbox_id=identity,state=data.get('last_event','unknown'),delivered=data.get('last_event')=='delivered')

    def send(self,identity):
        acquired=[]
        try:
            if self.store.pg:
                # Same ordering as the collector: prevent evidence/price changes during send validation.
                for key in (73541209,73541211):
                    self.store.execute('SELECT pg_advisory_lock(?)',(key,));acquired.append(key)
            return self._send(identity)
        finally:
            for key in reversed(acquired):self.store.execute('SELECT pg_advisory_unlock(?)',(key,))

    def _send(self,identity):
        if not self.settings.email_enabled or not self.settings.email_approval_id or self.settings.environment!='production':raise PermissionError('Production email disabled or not approved')
        if not os.getenv('RESEND_API_KEY'):raise ValueError('RESEND_API_KEY missing')
        row=self.store.execute('SELECT state,payload,event_ids,first_attempt_at FROM outbox WHERE id=?',(identity,)).fetchone()
        if not row:raise ValueError('Unknown outbox message')
        state,body,event_ids,first=row
        if state=='sent':return 'already_sent'
        if state=='cancelled':return 'cancelled_stale'
        if state=='failed':raise ValueError('Correct rejected email configuration; events are eligible for the next daily digest')
        now=self.clock()
        if first and now-dt.datetime.fromisoformat(first)>=dt.timedelta(hours=23):
            self.store.operation('reconcile:'+identity,'Resend result needs manual reconciliation; no automatic retry after idempotency safety window.')
            raise ReconciliationRequired('Check this message in Resend before retrying')
        unified=self.store.execute('SELECT 1 FROM discovery_reports WHERE outbox_id=?',(identity,)).fetchone()
        if unified:
            from .discovery.config import DiscoverySettings
            from .discovery.report import validate_frozen
            config=self.discovery_settings or DiscoverySettings.from_env()
            if not (config.enabled and config.daily_enabled and self.settings.discovery_source=='unified'):raise PermissionError('Unified reporting disabled')
            if not validate_frozen(self.store,self.boundary,identity,now,config):
                if first:raise ReconciliationRequired('Frozen unified report evidence changed; reconcile previous delivery')
                self.store.execute("UPDATE outbox SET state='cancelled',updated_at=? WHERE id=?",(now.isoformat(),identity));return 'cancelled_stale'
        rs_report=self.store.execute('SELECT 1 FROM realscout_reports WHERE outbox_id=?',(identity,)).fetchone()
        if rs_report:
            from .realscout_config import RealScoutSettings
            from .realscout_delivery import validate_frozen
            config=self.realscout_settings or RealScoutSettings.from_env()
            if not config.daily_enabled or self.settings.discovery_source!='realscout':
                raise PermissionError('RealScout report sending is not enabled')
            if not validate_frozen(self.store,self.boundary,identity,now):
                if first:raise ReconciliationRequired('Frozen report evidence changed; reconcile previous delivery before retrying')
                self.store.execute("UPDATE outbox SET state='cancelled',updated_at=? WHERE id=?",(now.isoformat(),identity))
                return 'cancelled_stale'
        envelope=json.loads(body)
        if envelope.get('from')!=self.settings.email_from or envelope.get('to')!=[self.settings.email_to]:
            raise PermissionError('Frozen email sender/recipient differ from approved configuration; prepare again before sending')
        # Criteria apply on every attempt, including uncertain-delivery retries.
        for event_id in json.loads(event_ids):
            ev=self.store.execute('SELECT property_id,revision,price FROM deal_events WHERE id=?',(event_id,)).fetchone()
            p=self.store.get_property(ev[0]) if ev else None
            cached=self.store.get_tax(ev[0],now.isoformat()) if ev else None
            valid=bool(p and p['state']=='Qualified' and p['source']==LIVE and p['revision']==ev[1] and p['payload'].get('price')==ev[2] and
                recent_timestamp(p['observed_at'],now) and cached and cached[1]==LIVE and cached[0].get('id')==ev[0] and not demo_marked(cached[0]) and
                verification(p['payload'],self.boundary)[0]=='Candidate' and tax_verification(cached[0],now.date())[0]=='Qualified')
            if not valid:
                if first:raise ReconciliationRequired('Saved email no longer meets current criteria; check previous delivery before proceeding')
                with self.store.transaction():
                    self.store.execute("UPDATE outbox SET state='cancelled' WHERE id=?",(identity,))
                    for e in json.loads(event_ids):self.store.execute("UPDATE deal_events SET state='ready' WHERE id=? AND state='queued'",(e,))
                self.store.operation('stale-email:'+identity,'Prepared digest became stale and was cancelled before sending.')
                return 'cancelled_stale'
        # Freeze the exact payload and idempotency key before any network attempt.
        self.store.execute("UPDATE outbox SET state='sending',first_attempt_at=COALESCE(first_attempt_at,?),updated_at=? WHERE id=?",(now.isoformat(),now.isoformat(),identity))
        from .resend_api import request as resend_request
        request=resend_request(body,os.environ['RESEND_API_KEY'],identity)
        try:
            with self.opener.open(request,timeout=self.settings.timeout) as response:
                result=json.load(response)
                if response.status not in {200,201} or not isinstance(result,dict) or not result.get('id'):
                    raise RuntimeError('Resend did not confirm message acceptance')
            with self.store.transaction():
                self.store.execute("UPDATE outbox SET state='sent',provider_id=?,updated_at=? WHERE id=?",(result['id'],self.clock().isoformat(),identity))
                for e in json.loads(event_ids):self.store.execute("UPDATE deal_events SET state='sent' WHERE id=?",(e,))
            event('digest_accepted',outbox_id=identity,properties=len(json.loads(event_ids)))
            return 'sent'
        except Exception as exc:
            evidence=None
            if isinstance(exc,urllib.error.HTTPError):
                from .resend_api import failure
                evidence=failure(exc)
                self.store.execute('INSERT INTO email_errors VALUES (?,?) ON CONFLICT(outbox_id) DO UPDATE SET payload=excluded.payload',(identity,json.dumps(evidence)))
                event('resend_request_rejected',outbox_id=identity,**evidence)
            definite=bool(isinstance(exc,urllib.error.HTTPError) and exc.code in {400,401,403,404,405,413,422}
                and not first and (exc.code!=403 or evidence['confirmed_rejection']))
            if definite:
                with self.store.transaction():
                    self.store.execute("UPDATE outbox SET state='failed',updated_at=? WHERE id=?",(self.clock().isoformat(),identity))
                    for e in json.loads(event_ids):self.store.execute("UPDATE deal_events SET state='ready' WHERE id=? AND state='queued'",(e,))
                self.store.operation('email-rejected:'+identity,'Resend rejected this message. Correct sender/recipient credentials; events remain eligible for the next day.')
                raise
            self.store.execute("UPDATE outbox SET state='uncertain',updated_at=? WHERE id=?",(self.clock().isoformat(),identity))
            self.store.operation('email:'+identity,'Daily email acceptance is uncertain; resume within 23 hours with the same idempotency key, or reconcile in Resend.')
            raise

    def reconcile(self,identity,provider_id):
        if not provider_id:raise ValueError('Provider message ID is required')
        row=self.store.execute('SELECT event_ids,state FROM outbox WHERE id=?',(identity,)).fetchone()
        if not row:raise ValueError('Unknown outbox message')
        with self.store.transaction():
            self.store.execute("UPDATE outbox SET state='sent',provider_id=?,updated_at=? WHERE id=?",(provider_id,self.clock().isoformat(),identity))
            for e in json.loads(row[0]):self.store.execute("UPDATE deal_events SET state='sent' WHERE id=?",(e,))
