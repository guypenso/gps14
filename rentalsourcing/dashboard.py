"""Read-only dashboard. Run: python -m rentalsourcing.dashboard (PORT defaults to 8080).

Use HTTPS at the hosting proxy. Enter DASHBOARD_TOKEN in the page; it is sent only
as an Authorization header, held in page memory, and never saved in browser storage.
"""
import datetime as dt
import hmac
import json
import os
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


class Dashboard:
    def __init__(self, database_url, token, production=True):
        if not token or len(token) < 32:
            raise ValueError('DASHBOARD_TOKEN must contain at least 32 characters')
        self.url, self.token = database_url, token
        self.pg = database_url.startswith(('postgres://', 'postgresql://'))
        if not self.pg and (production or not database_url.startswith('sqlite:///')):
            raise ValueError('Production dashboard requires PostgreSQL')

    def connect(self):
        if self.pg:
            import psycopg
            db = psycopg.connect(self.url, connect_timeout=10, client_encoding='utf8')
            db.execute('SET TRANSACTION READ ONLY')
            db.execute("SET LOCAL statement_timeout = '5000ms'")
            return db
        path = self.url[len('sqlite:///'):]
        from pathlib import Path
        return sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)

    def healthy(self):
        db = self.connect()
        try:
            # Health means the scanner schema is provisioned, not just a reachable DB.
            for table in ('properties', 'scan_jobs', 'outbox', 'usage'):
                db.execute('SELECT 1 FROM ' + table + ' LIMIT 1').fetchone()
        finally:
            db.close()

    def snapshot(self):
        now = dt.datetime.now(dt.timezone.utc)
        db = self.connect()
        try:
            observations = []
            for identity, payload, observed in db.execute('SELECT id,payload,observed_at FROM observations ORDER BY observed_at DESC LIMIT 200').fetchall():
                row = json.loads(payload)
                item = {key: row[key] for key in ('formattedAddress','price','bedrooms','bathrooms','squareFootage','latitude','longitude','_validation','validation','tax','qualification') if key in row}
                observations.append(dict(id=identity, observed_at=observed, **item))
            runs = [dict(id=i, **json.loads(p)) for i,p in db.execute('SELECT id,payload FROM runs LIMIT 100').fetchall()]
            notifications = []
            for identity,state,payload,updated in db.execute('SELECT id,state,payload,updated_at FROM notifications ORDER BY updated_at DESC LIMIT 200').fetchall():
                row = json.loads(payload)
                notifications.append(dict(id=identity,state=state,tax=row.get('tax'),updated_at=updated))
            properties = []
            from .core import tax_verification
            for identity,payload,state,reasons,source,observed,tax_payload,tax_source,fetched,expires in db.execute(
                'SELECT p.id,p.payload,p.state,p.reasons,p.source,p.observed_at,t.payload,t.source,t.fetched_at,t.expires_at FROM properties p LEFT JOIN tax_history t ON t.property_id=p.id ORDER BY p.observed_at DESC LIMIT 200').fetchall():
                row=json.loads(payload)
                item={key:row.get(key) for key in ('formattedAddress','price','bedrooms','bathrooms','squareFootage')}
                stored_state = state
                effective_reasons = json.loads(reasons)
                def timestamp(value):
                    try:
                        parsed = dt.datetime.fromisoformat(value.replace('Z','+00:00'))
                        return parsed if parsed.tzinfo is not None else None
                    except (ValueError, TypeError, AttributeError):
                        return None
                observed_time, fetched_time, expiry = map(timestamp, (observed, fetched, expires))
                freshness_reasons = []
                if not observed_time or not dt.timedelta(0) <= now-observed_time <= dt.timedelta(hours=24):
                    freshness_reasons.append('stale_or_invalid_listing_timestamp')
                if source != 'rentcast-live': freshness_reasons.append('non_live_listing_source')
                tax_state, tax, _ = tax_verification(json.loads(tax_payload), now.date()) if tax_payload else ('Pending Verification', None, None)
                tax_current = bool(tax_payload and tax_source == 'rentcast-live' and fetched_time and fetched_time <= now and expiry and expiry > now)
                if not tax_current:
                    freshness_reasons.append('missing_expired_or_non_live_tax')
                    tax = None
                elif tax_state != 'Qualified' and stored_state == 'Qualified':
                    freshness_reasons.append('tax_no_longer_qualified')
                if stored_state == 'Qualified' and freshness_reasons:
                    state = 'Pending Verification'
                effective_reasons = list(dict.fromkeys(effective_reasons + freshness_reasons))
                properties.append(dict(id=identity,**item,state=state,stored_state=stored_state,reasons=effective_reasons,source=source,observed_at=observed,tax=tax,tax_current=tax_current,tax_source=tax_source,tax_fetched_at=fetched,tax_expires_at=expires))
            jobs=[dict(id=i,day=d,state=state,stats=json.loads(stats),updated_at=updated) for i,d,state,stats,updated in db.execute('SELECT id,day,state,stats,updated_at FROM scan_jobs ORDER BY updated_at DESC LIMIT 100').fetchall()]
            outbox=[dict(id=i,state=state,updated_at=updated) for i,state,updated in db.execute('SELECT id,state,updated_at FROM outbox ORDER BY updated_at DESC LIMIT 100').fetchall()]
            usage = dict(db.execute('SELECT period,used FROM usage').fetchall())
            return dict(properties=properties,jobs=jobs,outbox=outbox,observations=observations,runs=runs,notifications=notifications,usage=usage,
                        note='Stored observations are not necessarily qualified deals. Tax and city validation must be recorded explicitly. Displayed Qualified state requires live sources, a listing observed within 24 hours and unexpired verified tax. Stored state is retained separately; this page does not recheck city geometry or fetch new listings. Results are limited to 200 properties/observations and 100 jobs/runs.')
        finally:
            db.close()


PAGE = b'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>RentalSourcing</title>
<style>body{font:16px system-ui;max-width:1100px;margin:40px auto;padding:0 20px;background:#f5f7fa;color:#172033}input,button{padding:10px}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:white;padding:20px;border-radius:8px}small{display:block;margin:15px 0}</style>
<h1>RentalSourcing</h1><p>Live stored scanner results</p><form id="login"><label>Dashboard token <input id="token" type="password" required autocomplete="off"></label> <button>Connect</button></form>
<small>Results refresh every 60 seconds while this page is open. Observations alone do not confirm qualification.</small><p id="status">Authentication required</p><main id="results"></main><script src="/app.js"></script></html>'''
SCRIPT = b'''let secret='';let pending=false;
async function refresh(){if(!secret||pending)return;pending=true;try{const r=await fetch('/api/status',{headers:{Authorization:'Bearer '+secret},cache:'no-store'});if(!r.ok)throw Error(r.status===401?'Invalid token':'Service unavailable');const data=await r.json();const root=document.getElementById('results');root.replaceChildren();const title=document.createElement('h2');title.textContent='Properties';root.append(title);const table=document.createElement('table');for(const values of [['Address','Price','State','Reasons','Annual tax + $100','Tax source','Listing observed','Tax expires'],...data.properties.map(p=>[p.formattedAddress,p.price,p.state,p.reasons.join(', '),p.tax?p.tax.adjusted:'Unverified',p.tax_source||'Unavailable',p.observed_at,p.tax_expires_at||'Unavailable'])]){const tr=document.createElement('tr');for(const value of values){const td=document.createElement('td');td.textContent=value===null?'Unknown':String(value);td.style.padding='10px';tr.append(td)}table.append(tr)}root.append(table);for(const key of ['note','jobs','runs','usage','outbox','observations','notifications']){const h=document.createElement('h2');h.textContent=key;const p=document.createElement('pre');p.textContent=JSON.stringify(data[key],null,2);root.append(h,p)}document.getElementById('status').textContent='Updated '+new Date().toLocaleString()}catch(e){document.getElementById('status').textContent=e.message}finally{pending=false}}
document.getElementById('login').addEventListener('submit',e=>{e.preventDefault();secret=document.getElementById('token').value;document.getElementById('token').value='';refresh()});setInterval(refresh,60000);'''


def handler_for(app):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            # Do not log request URLs or Authorization headers.
            pass

        def reply(self, code, payload, kind='application/json'):
            if not isinstance(payload, bytes): payload = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header('Content-Type', kind + '; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'")
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            route = urlsplit(self.path)
            if route.query:
                return self.reply(400, {'error':'Query parameters are not accepted'})
            if route.path == '/': return self.reply(200, PAGE, 'text/html')
            if route.path == '/app.js': return self.reply(200, SCRIPT, 'application/javascript')
            if route.path == '/healthz':
                try: app.healthy()
                except Exception: return self.reply(503, {'status':'unavailable'})
                return self.reply(200, {'status':'ok'})
            if route.path != '/api/status': return self.reply(404, {'error':'Not found'})
            auth = self.headers.get('Authorization', '')
            if not hmac.compare_digest(auth.encode(), ('Bearer ' + app.token).encode()):
                return self.reply(401, {'error':'Authentication required'})
            try: snapshot = app.snapshot()
            except Exception: return self.reply(503, {'error':'Database unavailable'})
            return self.reply(200, snapshot)
    return Handler


def main():
    app = Dashboard(os.environ['DATABASE_URL'], os.environ['DASHBOARD_TOKEN'], production=os.getenv('APP_ENV') != 'development')
    from .storage import Store
    store = Store(app.url)
    try: store.migrate()
    finally: store.close()
    server = ThreadingHTTPServer(('0.0.0.0', int(os.getenv('PORT','8080'))), handler_for(app))
    try: server.serve_forever()
    finally: server.server_close()


if __name__ == '__main__': main()
