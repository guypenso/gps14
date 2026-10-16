import datetime as dt
import json
import sqlite3

class BudgetExceeded(RuntimeError): pass

class Store:
    def __init__(self, url):
        self.pg = url.startswith(('postgres://', 'postgresql://'))
        if self.pg:
            import psycopg
            self.db = psycopg.connect(url, autocommit=True, connect_timeout=15, client_encoding='utf8')
        elif url.startswith('sqlite:///'):
            self.db = sqlite3.connect(url[len('sqlite:///'):], isolation_level=None)
        else: raise ValueError('DATABASE_URL must be PostgreSQL or local sqlite:/// path')
        for statement in [
            'CREATE TABLE IF NOT EXISTS usage (period TEXT PRIMARY KEY, used INTEGER NOT NULL)',
            'CREATE TABLE IF NOT EXISTS observations (id TEXT PRIMARY KEY, payload TEXT NOT NULL, observed_at TEXT NOT NULL)',
            'CREATE TABLE IF NOT EXISTS notifications (id TEXT PRIMARY KEY, state TEXT NOT NULL, payload TEXT NOT NULL, updated_at TEXT NOT NULL)',
            'CREATE TABLE IF NOT EXISTS scheduled_days (day TEXT PRIMARY KEY)',
            'CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, payload TEXT NOT NULL)']:
            self.execute(statement)

    def execute(self, sql, args=()):
        return self.db.execute(sql.replace('?', '%s') if self.pg else sql, args)

    def lock(self):
        # Session-scoped: released on connection close, including process death.
        if self.pg:
            return bool(self.execute('SELECT pg_try_advisory_lock(73541209)').fetchone()[0])
        return True  # SQLite is local testing only; cloud requires PostgreSQL.

    def reserve(self, day_limit, month_limit, now=None, total_limit=40):
        now = now or dt.datetime.now(dt.timezone.utc)
        periods = [(now.strftime('day:%Y-%m-%d'), day_limit), (now.strftime('month:%Y-%m'), month_limit), ('all_time', total_limit)]
        self.execute('BEGIN' if self.pg else 'BEGIN IMMEDIATE')
        try:
            for period, limit in periods:
                self.execute('INSERT INTO usage(period, used) VALUES (?,0) ON CONFLICT(period) DO NOTHING', (period,))
                c = self.execute('UPDATE usage SET used=used+1 WHERE period=? AND used<? RETURNING used', (period,limit))
                if not c.fetchone(): raise BudgetExceeded('Persistent API budget reached')
            self.execute('COMMIT')
        except Exception:
            self.execute('ROLLBACK'); raise

    def claim_scheduled_day(self, day):
        return self.execute('INSERT INTO scheduled_days VALUES (?) ON CONFLICT(day) DO NOTHING RETURNING day', (day,)).fetchone() is not None

    def observe(self, row):
        self.execute('INSERT INTO observations VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, observed_at=excluded.observed_at',
                     (row['id'],json.dumps(row),dt.datetime.now(dt.timezone.utc).isoformat()))

    def claimed(self, identity):
        return self.execute('SELECT state FROM notifications WHERE id=?', (identity,)).fetchone() is not None

    def claim(self, identity, payload):
        return self.execute('INSERT INTO notifications VALUES (?,?,?,?) ON CONFLICT(id) DO NOTHING RETURNING id',
                            (identity,'reserved',json.dumps(payload),dt.datetime.now(dt.timezone.utc).isoformat())).fetchone() is not None

    def mark(self, identity, state):
        self.execute('UPDATE notifications SET state=?, updated_at=? WHERE id=?',
                     (state,dt.datetime.now(dt.timezone.utc).isoformat(),identity))

    def run(self, identity, payload):
        self.execute('INSERT INTO runs VALUES (?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload', (identity,json.dumps(payload)))

    def close(self): self.db.close()

    # Additive v2 schema preserves all observations/notifications from the initial release.
    def migrate(self):
        statements=[
          'CREATE TABLE IF NOT EXISTS schema_versions (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)',
          'CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, payload TEXT NOT NULL)',
          'CREATE TABLE IF NOT EXISTS properties (id TEXT PRIMARY KEY, payload TEXT NOT NULL, state TEXT NOT NULL, reasons TEXT NOT NULL, source TEXT NOT NULL, revision INTEGER NOT NULL, observed_at TEXT NOT NULL)',
          'CREATE TABLE IF NOT EXISTS price_history (id TEXT PRIMARY KEY, property_id TEXT NOT NULL, price DOUBLE PRECISION NOT NULL, previous_price DOUBLE PRECISION, reduced INTEGER NOT NULL, observed_at TEXT NOT NULL)',
          'CREATE TABLE IF NOT EXISTS tax_history (property_id TEXT PRIMARY KEY, payload TEXT NOT NULL, source TEXT NOT NULL, fetched_at TEXT NOT NULL, expires_at TEXT NOT NULL)',
          'CREATE TABLE IF NOT EXISTS response_cache (key TEXT PRIMARY KEY, payload TEXT NOT NULL, source TEXT NOT NULL, expires_at TEXT NOT NULL)',
          'CREATE TABLE IF NOT EXISTS scan_jobs (id TEXT PRIMARY KEY, day TEXT NOT NULL, state TEXT NOT NULL, offset_value INTEGER NOT NULL, collection_done INTEGER NOT NULL, stats TEXT NOT NULL, updated_at TEXT NOT NULL)',
          'CREATE TABLE IF NOT EXISTS scan_tasks (job_id TEXT NOT NULL, property_id TEXT NOT NULL, done INTEGER NOT NULL, priority INTEGER NOT NULL, event_id TEXT, PRIMARY KEY(job_id,property_id))',
          'CREATE TABLE IF NOT EXISTS deal_events (id TEXT PRIMARY KEY, property_id TEXT NOT NULL, kind TEXT NOT NULL, price DOUBLE PRECISION NOT NULL, previous_price DOUBLE PRECISION, revision INTEGER NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL)',
          'CREATE TABLE IF NOT EXISTS outbox (id TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE, state TEXT NOT NULL, payload TEXT NOT NULL, event_ids TEXT NOT NULL, provider_id TEXT, first_attempt_at TEXT, updated_at TEXT NOT NULL)',
          'CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, message TEXT NOT NULL, created_at TEXT NOT NULL)',
          'CREATE INDEX IF NOT EXISTS scan_tasks_pending ON scan_tasks(job_id,done,priority)',
          'CREATE INDEX IF NOT EXISTS deal_events_property ON deal_events(property_id,state)']
        with self.transaction():
            for statement in statements:self.execute(statement)
            self.execute('INSERT INTO schema_versions VALUES (2,?) ON CONFLICT(version) DO NOTHING',(self.now(),))

    @staticmethod
    def now():return dt.datetime.now(dt.timezone.utc).isoformat()

    def transaction(self):
        from contextlib import contextmanager
        @contextmanager
        def tx():
            self.execute('BEGIN' if self.pg else 'BEGIN IMMEDIATE')
            try:yield;self.execute('COMMIT')
            except BaseException:self.execute('ROLLBACK');raise
        return tx()

    def save_setting(self,name,value):
        self.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(name) DO UPDATE SET payload=excluded.payload',(name,json.dumps(value)))

    def operation(self,key,message):
        self.execute('INSERT INTO operations VALUES (?,?,?) ON CONFLICT(id) DO NOTHING',(key,message,self.now()))

    def operations(self):
        return [f'[{r[0]}] {r[1]}' for r in self.execute('SELECT created_at,message FROM operations ORDER BY created_at DESC LIMIT 10').fetchall()]

    def count_usage(self,period):
        row=self.execute('SELECT used FROM usage WHERE period=?',(period,)).fetchone()
        return row[0] if row else 0

    def reserve_api(self,settings,now=None):
        import calendar
        now=now or dt.datetime.now(dt.timezone.utc)
        if not settings.approval_id or settings.approved_requests<=0:raise BudgetExceeded('No approved live API allowance')
        cycle=now.date().replace(day=settings.billing_day)
        if now.date()<cycle:
            year,month=(cycle.year-1,12) if cycle.month==1 else (cycle.year,cycle.month-1)
            cycle=cycle.replace(year=year,month=month)
        periods=[(now.strftime('day:%Y-%m-%d'),settings.per_day),
                 ('cycle:'+cycle.isoformat(),settings.per_month),
                 ('approval:'+settings.approval_id,settings.approved_requests)]
        with self.transaction():
            for key,limit in periods:
                self.execute('INSERT INTO usage VALUES (?,0) ON CONFLICT(period) DO NOTHING',(key,))
                if not self.execute('UPDATE usage SET used=used+1 WHERE period=? AND used<? RETURNING used',(key,limit)).fetchone():
                    raise BudgetExceeded('API allowance reached: '+key.split(':')[0])
        return self.budget_health(settings,now)

    def budget_health(self,settings,now=None):
        import calendar
        now=now or dt.datetime.now(dt.timezone.utc)
        cycle=now.date().replace(day=settings.billing_day)
        if now.date()<cycle:
            y,m=(cycle.year-1,12) if cycle.month==1 else (cycle.year,cycle.month-1)
            cycle=cycle.replace(year=y,month=m)
        y,m=(cycle.year+1,1) if cycle.month==12 else (cycle.year,cycle.month+1)
        end=cycle.replace(year=y,month=m)
        used=self.count_usage('cycle:'+cycle.isoformat())
        remaining=max(0,min(settings.per_month-used,settings.approved_requests-self.count_usage('approval:'+settings.approval_id)))
        days=max(1,(end-now.date()).days)
        warning=None
        if remaining<days:warning='API allowance cannot cover even one listing request per remaining day; increase only after approval.'
        elif used>=settings.per_month*.85:warning='API billing-cycle allowance is at least 85% consumed.'
        if warning:self.operation('budget:'+cycle.isoformat()+':'+str(remaining),warning)
        return dict(cycle_start=cycle.isoformat(),used=used,remaining=remaining,days_remaining=days,warning=warning)

    def cache_get(self,key,now):
        row=self.execute('SELECT payload,source FROM response_cache WHERE key=? AND expires_at>?',(key,now)).fetchone()
        return (json.loads(row[0]),row[1]) if row else None

    def cache_put(self,key,payload,source,expires):
        self.execute('INSERT INTO response_cache VALUES (?,?,?,?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload,source=excluded.source,expires_at=excluded.expires_at',
                     (key,json.dumps(payload),source,expires))

    def get_property(self,identity):
        r=self.execute('SELECT payload,state,reasons,source,revision,observed_at FROM properties WHERE id=?',(identity,)).fetchone()
        return dict(payload=json.loads(r[0]),state=r[1],reasons=json.loads(r[2]),source=r[3],revision=r[4],observed_at=r[5]) if r else None

    def save_property(self,identity,payload,state,reasons,source,revision,observed_at):
        self.execute('INSERT INTO properties VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,state=excluded.state,reasons=excluded.reasons,source=excluded.source,revision=excluded.revision,observed_at=excluded.observed_at',
                     (identity,json.dumps(payload),state,json.dumps(reasons),source,revision,observed_at))

    def set_verification(self,identity,state,reasons):
        self.execute('UPDATE properties SET state=?,reasons=? WHERE id=?',(state,json.dumps(reasons),identity))

    def get_tax(self,identity,now):
        r=self.execute('SELECT payload,source,fetched_at FROM tax_history WHERE property_id=? AND expires_at>?',(identity,now)).fetchone()
        return (json.loads(r[0]),r[1],r[2]) if r else None

    def save_tax(self,identity,payload,source,fetched,expires):
        self.execute('INSERT INTO tax_history VALUES (?,?,?,?,?) ON CONFLICT(property_id) DO UPDATE SET payload=excluded.payload,source=excluded.source,fetched_at=excluded.fetched_at,expires_at=excluded.expires_at',
                     (identity,json.dumps(payload),source,fetched,expires))

    def job(self,identity,day):
        self.execute('INSERT INTO scan_jobs VALUES (?,?,?,0,0,?,?) ON CONFLICT(id) DO NOTHING',(identity,day,'running','{}',self.now()))
        r=self.execute('SELECT state,offset_value,collection_done,stats FROM scan_jobs WHERE id=?',(identity,)).fetchone()
        return dict(id=identity,state=r[0],offset=r[1],collection_done=bool(r[2]),stats=json.loads(r[3]))

    def checkpoint(self,job_id,offset,done,stats,state='running'):
        self.execute('UPDATE scan_jobs SET state=?,offset_value=?,collection_done=?,stats=?,updated_at=? WHERE id=?',
                     (state,offset,int(done),json.dumps(stats),self.now(),job_id))
