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
from .core import demo_marked,verification,tax_verification
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
    lines=[f'RentalSourcing — {day}',f'מצב הסריקה: {status}']
    items=sorted(items,key=lambda item:item['kind']!='price_reduction')
    cards=[]
    for item in items:
        p=item['listing'];t=item['tax'];previous=item.get('previous_price')
        label='הורדת מחיר חדשה' if item['kind']=='price_reduction' else 'נכס חדש שאומת'
        map_url='https://www.google.com/maps/search/?'+urllib.parse.urlencode({'api':1,'query':p['formattedAddress']})
        text=f"{label}: {p['formattedAddress']}\nמחיר: {money(p['price'])}"
        if previous is not None and previous>p['price']:
            text+=f" | קודם: {money(previous)} | ירידה: {money(previous-p['price'])}"
        text+=f"\nחדרי שינה: {p['bedrooms']} | אמבטיות: {p['bathrooms']} | שטח: {p['squareFootage']} sqft | ZIP: {p['zipCode']}"
        text+=f"\nמס שנתי לשנת {t['year']}: {money(t['annual'])} + $100 = {money(t['adjusted'])}"
        text+=f"\nמקור: RentCast Sale Listings | מזהה: {p['id']}\nאימות: {item['verified_at']} | נתוני מס נשלפו: {item['tax_fetched_at']}\nמפה: {map_url}"
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
    def __init__(self,store,boundary,settings,clock=None,opener=None):
        self.store=store;self.boundary=boundary;self.settings=settings
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

    def send(self,identity):
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
        if not first:
            for event_id in json.loads(event_ids):
                ev=self.store.execute('SELECT property_id,revision,price FROM deal_events WHERE id=?',(event_id,)).fetchone()
                p=self.store.get_property(ev[0]) if ev else None
                cached=self.store.get_tax(ev[0],now.isoformat()) if ev else None
                valid=bool(p and p['state']=='Qualified' and p['source']==LIVE and p['revision']==ev[1] and p['payload'].get('price')==ev[2] and
                    recent_timestamp(p['observed_at'],now) and cached and cached[1]==LIVE and cached[0].get('id')==ev[0] and not demo_marked(cached[0]) and
                    verification(p['payload'],self.boundary)[0]=='Candidate' and tax_verification(cached[0],now.date())[0]=='Qualified')
                if not valid:
                    with self.store.transaction():
                        self.store.execute("UPDATE outbox SET state='cancelled' WHERE id=?",(identity,))
                        for e in json.loads(event_ids):self.store.execute("UPDATE deal_events SET state='ready' WHERE id=? AND state='queued'",(e,))
                    self.store.operation('stale-email:'+identity,'Prepared digest became stale and was cancelled before sending.')
                    return 'cancelled_stale'
        # Freeze the exact payload and idempotency key before any network attempt.
        self.store.execute("UPDATE outbox SET state='sending',first_attempt_at=COALESCE(first_attempt_at,?),updated_at=? WHERE id=?",(now.isoformat(),now.isoformat(),identity))
        request=urllib.request.Request('https://api.resend.com/emails',method='POST',data=body.encode(),headers={
           'Authorization':'Bearer '+os.environ['RESEND_API_KEY'],'Content-Type':'application/json','Idempotency-Key':identity})
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
            if isinstance(exc,urllib.error.HTTPError) and exc.code in {400,401,403,404,405,413,422}:
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
