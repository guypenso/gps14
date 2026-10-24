"""Private read-only dashboard. HTTP routes never call RentCast or send email."""
import datetime as dt
import hmac
import json
import logging
import os
from pathlib import Path
from urllib.parse import urlencode
from flask import Flask,jsonify,render_template,request,Response
from .collector import LIVE
from .core import verification,tax_verification,demo_marked,number
from .digest import render as render_digest
from .geography import load_boundary
from .poc import safe_listing_link
from .storage import Store
from .telemetry import event

LABELS={'Qualified':'מאומת','Pending Verification':'ממתין לאימות','Rejected':'נפסל'}
REASONS={'excluded_zip':'מיקוד מוחרג','price':'מחיר חסר או מחוץ לתקרה','bedrooms':'מספר חדרי שינה חסר או נמוך מ־3',
 'bathrooms':'מספר אמבטיות חסר או נמוך מ־1','squareFootage':'שטח חסר או נמוך מ־1,000 sqft',
 'outside_city_or_boundary':'מחוץ לגבול העיר או על הגבול','coordinates':'קואורדינטות חסרות או לא תקינות',
 'annual_tax_too_high':'המס השנתי בתוספת $100 אינו נמוך מ־$2,000','missing_tax_history':'היסטוריית מס חסרה',
 'invalid_latest_tax_amount':'סכום המס האחרון חסר או לא תקין','missing_tax_year':'שנת מס חסרה',
 'tax_not_verified':'נתוני המס טרם אומתו','stale_listing':'אימות המודעה ישן מ־24 שעות',
 'tax_cache_expired':'נדרש רענון נתוני המס','city_boundary_unavailable':'גבול העיר הרשמי אינו זמין לאימות',
 'tax_identity_or_source_mismatch':'נתוני המס אינם תואמים בוודאות לנכס',
 'tax_refresh_not_needed_no_new_event':'המס דורש רענון; לא נשלחה התראה חדשה',
 'non_live_source':'מקור הנתונים לא אומת','zipCode':'מיקוד חסר','address':'כתובת חסרה','property_id':'מזהה נכס חסר',
 'invalid_timestamp':'מועד האימות חסר או לא תקין','future_timestamp':'מועד האימות נמצא בעתיד',
 'status':'המודעה אינה פעילה למכירה','state':'הנכס אינו בפנסילבניה'}

class StructuralBoundary:
    def contains(self,row):return True


def timestamp(value):
    try:
        parsed=dt.datetime.fromisoformat(value.replace('Z','+00:00'))
        return parsed if parsed.tzinfo is not None else None
    except (ValueError,TypeError,AttributeError):return None


def snapshot(store,boundary,now):
    rows=store.execute('SELECT p.id,p.payload,p.state,p.reasons,p.source,p.observed_at,p.revision,t.payload,t.source,t.fetched_at,t.expires_at FROM properties p LEFT JOIN tax_history t ON p.id=t.property_id WHERE p.source=? ORDER BY p.observed_at DESC,p.id LIMIT 10001',(LIVE,)).fetchall()
    truncated=len(rows)>10000;items=[];counts={s:0 for s in LABELS};failures={}
    histories={}
    for r in store.execute('SELECT property_id,price,previous_price,reduced,observed_at FROM (SELECT property_id,price,previous_price,reduced,observed_at,ROW_NUMBER() OVER (PARTITION BY property_id ORDER BY observed_at DESC,id DESC) AS rn FROM price_history) h WHERE rn<=20 ORDER BY observed_at DESC').fetchall():
        histories.setdefault(r[0],[]).append(dict(price=r[1],previous_price=r[2],reduced=bool(r[3]),observed_at=r[4]))
    reductions={r[0]:(r[1],r[2]) for r in store.execute("SELECT e.property_id,e.revision,e.previous_price FROM deal_events e JOIN properties p ON p.id=e.property_id AND p.revision=e.revision WHERE e.kind='price_reduction' AND p.source=?",(LIVE,)).fetchall()}
    for identity,payload,state,reasons,source,observed,revision,tax_json,tax_source,tax_fetched,tax_expires in rows[:10000]:
        p=json.loads(payload)
        if demo_marked(p):continue
        stored_state=state
        reasons=json.loads(reasons);tax=None;tax_current=False
        basic,problems=verification(p,boundary or StructuralBoundary())
        if basic=='Rejected':state='Rejected';reasons=problems
        elif basic=='Pending Verification':state=basic;reasons=problems
        else:
            missing=[]
            if boundary is None:missing.append('city_boundary_unavailable')
            observed_time=timestamp(observed)
            if not observed_time:missing.append('invalid_timestamp')
            elif observed_time>now:missing.append('future_timestamp')
            elif now-observed_time>dt.timedelta(hours=24):missing.append('stale_listing')
            if tax_json and tax_source==LIVE:
                record=json.loads(tax_json)
                if record.get('id')==identity and not demo_marked(record):
                    tax_state,tax,tax_reason=tax_verification(record,now.date())
                    if tax_reason:missing.append(tax_reason)
                    if tax_state=='Rejected':state='Rejected';reasons=[tax_reason]
                else:missing.append('tax_identity_or_source_mismatch')
            else:missing.append('missing_tax_history')
            expiry=timestamp(tax_expires);fetched=timestamp(tax_fetched)
            tax_current=bool(tax and expiry and expiry>now and fetched and fetched<=now)
            if not tax_current:
                missing.append('tax_cache_expired');tax=None
            if state!='Rejected' and missing:state='Pending Verification';reasons=list(dict.fromkeys(reasons+missing))
            # HTTP reads can downgrade stale data; only the collection engine may approve a property.
        if state not in counts:state='Pending Verification'
        counts[state]+=1
        if state!='Qualified':
            for reason in reasons:failures[reason]=failures.get(reason,0)+1
        history=histories.get(identity,[])
        reduced=bool(history and history[0]['reduced'] and number(p.get('price')) and history[0]['price']==p['price']);previous=history[0]['previous_price'] if history else None
        ev=reductions.get(identity)
        if not reduced and ev and ev[0]==revision and number(ev[1]) and number(p.get('price')) and ev[1]>p['price']:
            reduced=True;previous=ev[1]
        items.append(dict(id=identity,address=p.get('formattedAddress') or 'כתובת חסרה',price=p.get('price'),bedrooms=p.get('bedrooms'),
          bathrooms=p.get('bathrooms'),squareFootage=p.get('squareFootage'),zipCode=p.get('zipCode'),state=state,stored_state=stored_state,label=LABELS[state],
          reasons=reasons,reason_text=[REASONS.get(x,x) for x in reasons],source='RentCast Sale Listings',
          observed_at=observed,tax_year=tax['year'] if tax else None,annual_tax=tax['annual'] if tax else None,
          adjusted_tax=tax['adjusted'] if tax else None,tax_current=tax_current,tax_expires_at=tax_expires,tax_fetched_at=tax_fetched,tax_source='RentCast Property Records' if tax else None,
          price_reduction=reduced,previous_price=previous,price_history=history,listing_url=safe_listing_link(p),
          map_url='https://www.google.com/maps/search/?'+urlencode({'api':1,'query':p.get('formattedAddress','')})))
    items.sort(key=lambda p:(not p['price_reduction'],p['state']!='Qualified',p['address']))
    jobs=[dict(id=r[0],day=r[1],state=r[2],stats=json.loads(r[3]),updated_at=r[4]) for r in store.execute('SELECT id,day,state,stats,updated_at FROM scan_jobs ORDER BY updated_at DESC LIMIT 10').fetchall()]
    usage=[dict(period=r[0],attempts=r[1]) for r in store.execute('SELECT period,used FROM usage ORDER BY period LIMIT 100').fetchall()]
    outbox=[dict(id=r[0],state=r[1],provider_id=r[2],updated_at=r[3]) for r in store.execute('SELECT id,state,provider_id,updated_at FROM outbox ORDER BY updated_at DESC LIMIT 10').fetchall()]
    item_map={p['id']:p for p in items}
    events=[]
    for identity,property_id,kind,price,previous,state,payload,created in store.execute('SELECT id,property_id,kind,price,previous_price,state,payload,created_at FROM deal_events ORDER BY created_at DESC LIMIT 100').fetchall():
        payload=json.loads(payload);p=item_map.get(property_id)
        if not p or payload.get('source')!=LIVE or payload.get('tax_source')!=LIVE or demo_marked(payload.get('listing',{})):continue
        events.append(dict(id=identity,property_id=property_id,address=p['address'],kind=kind,price=price,previous_price=previous,state=state,created_at=created,current_property_state=p['state']))
    successful=store.execute("SELECT id,day,state,stats,updated_at FROM scan_jobs WHERE state='complete' ORDER BY updated_at DESC LIMIT 1").fetchone()
    last_successful_scan=dict(id=successful[0],day=successful[1],state=successful[2],stats=json.loads(successful[3]),updated_at=successful[4]) if successful else None
    failed_runs=[]
    for identity,payload in store.execute('SELECT id,payload FROM runs').fetchall():
        run=json.loads(payload)
        if run.get('status')=='failed':failed_runs.append(dict(id=identity,**run))
    return dict(last_successful_scan=last_successful_scan,failed_runs=failed_runs[-20:],new_deals=[e for e in events if e['kind']=='new'],price_reductions=[e for e in events if e['kind']=='price_reduction'],properties=items,counts=counts,total=len(items),truncated=truncated,rejection_reasons=failures,
      boundary_verified=boundary is not None,jobs=jobs,usage=usage,outbox=outbox,operations=store.operations(),
      coverage=dict(state='לא נמדד',percent=None,sample_size=0),generated_at=now.isoformat())


def create_app(database_url=None,*,testing=False,clock=None,boundary=None):
    app=Flask(__name__);app.config.update(TESTING=testing,MAX_CONTENT_LENGTH=131072,PROPAGATE_EXCEPTIONS=False)
    from .realscout_config import RealScoutSettings
    rs_settings=RealScoutSettings.from_env()
    from .discovery.config import DiscoverySettings
    discovery_settings=DiscoverySettings.from_env()
    url=database_url or os.getenv('DATABASE_URL','')
    if not testing:
        from .database import validate_postgres_url
        validate_postgres_url(url)
    username=os.getenv('DASHBOARD_USERNAME','owner');password=os.getenv('DASHBOARD_PASSWORD','')
    if len(password)<20:raise ValueError('Set DASHBOARD_PASSWORD to at least 20 characters')
    clock=clock or (lambda:dt.datetime.now(dt.timezone.utc))
    if boundary is None:
        try:boundary=load_boundary(os.getenv('CITY_BOUNDARY_FILE','data/pittsburgh.geojson'))
        except (OSError,ValueError,KeyError):event('dashboard_boundary_unavailable')
    if not testing:
        s=Store(url,initialize=False)
        try:
            if s.execute('SELECT version FROM schema_versions WHERE version=2').fetchone() is None:
                raise RuntimeError('Database migrations are missing; run scripts/start_web.sh')
        finally:s.close()

    @app.before_request
    def authenticate():
        if request.path=='/healthz' and request.method in {'GET','HEAD'}:return None
        # Only this exact POST route uses webhook signatures instead of dashboard Basic auth.
        if request.path=='/webhooks/resend/inbound' and request.method=='POST':return None
        if request.path.startswith('/webhooks/mls/export/') and request.method=='POST':return None  # Dedicated HMAC route authenticates itself.
        auth=request.authorization
        valid=bool(auth and hmac.compare_digest((auth.username or '').encode(),username.encode()) & hmac.compare_digest((auth.password or '').encode(),password.encode()))
        if not valid:return Response('נדרשת כניסה לדשבורד',401,{'WWW-Authenticate':'Basic realm="RentalSourcing", charset="UTF-8"'},content_type='text/plain; charset=utf-8')

    @app.after_request
    def secure_headers(response):
        response.headers['Cache-Control']='no-store'
        response.headers['X-Content-Type-Options']='nosniff';response.headers['X-Frame-Options']='DENY'
        response.headers['Referrer-Policy']='no-referrer'
        response.headers['Content-Security-Policy']="default-src 'self'; style-src 'self'; script-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        response.headers['Strict-Transport-Security']='max-age=31536000'
        return response

    def data():
        s=Store(url,initialize=False)
        try:return snapshot(s,boundary,clock())
        finally:s.close()

    @app.get('/healthz')
    def health():
        try:
            s=Store(url,initialize=False)
            try:
                if s.execute('SELECT version FROM schema_versions WHERE version=2').fetchone() is None:
                    raise ValueError('Database migrations are missing')
            finally:s.close()
            return jsonify(status='ok')
        except Exception as exc:
            event('dashboard_database_unavailable',error_type=type(exc).__name__)
            return jsonify(status='unavailable'),503

    @app.get('/readyz')
    def ready():
        h=health()
        if isinstance(h,tuple):return h
        return (jsonify(status='ready' if boundary else 'boundary_missing',boundary_verified=boundary is not None),200 if boundary else 503)

    @app.get('/')
    def index():
        d=data();state=request.args.get('state','all')
        selected=[p for p in d['properties'] if state=='all' or p['state']==state]
        try:page=max(1,int(request.args.get('page','1')))
        except ValueError:page=1
        return render_template('dashboard.html',data=d,properties=selected[(page-1)*100:page*100],state=state,page=page,has_next=len(selected)>page*100,testing=testing)

    @app.get('/api/dashboard')
    def api():return jsonify(data())

    @app.get('/api/email-preview')
    def email_preview():
        s=Store(url,initialize=False)
        try:
            record=s.execute('SELECT payload,state FROM outbox ORDER BY updated_at DESC LIMIT 1').fetchone()
            if record:
                body=json.loads(record[0]);return jsonify(mode='preview_only_no_send',state=record[1],subject=body.get('subject'),text=body.get('text'))
            return jsonify(mode='preview_only_no_send',state='not_prepared',subject='RentalSourcing',text='טרם הוכן מייל יומי. אין שליחת מייל מתוך הדשבורד.')
        finally:s.close()

    @app.errorhandler(500)
    def unavailable(error):return jsonify(error='הנתונים אינם זמינים כרגע; בדוק את לוג השירות'),503

    from .realscout_web import register
    register(app,lambda:Store(url,initialize=False),rs_settings,clock,boundary)
    from .discovery.export_web import register as register_exports
    register_exports(app,lambda:Store(url,initialize=False),discovery_settings,clock)
    from .discovery.web import register as register_discovery
    register_discovery(app,lambda:Store(url,initialize=False),discovery_settings,clock,boundary)
    if rs_settings.worker_enabled and not testing:
        from .realscout_worker import start_background
        app.extensions['realscout_worker']=start_background(lambda:Store(url,initialize=False),rs_settings)
    return app
