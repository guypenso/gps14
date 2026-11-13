import datetime as dt
import json
import sqlite3

class BudgetExceeded(RuntimeError): pass

class Store:
    def __init__(self, url, *, initialize=True):
        self.pg = url.startswith(('postgres://', 'postgresql://'))
        if self.pg:
            from .database import connect_postgres
            self.db = connect_postgres(url)
            self.db.execute('SET statement_timeout=30000')
            self.db.execute('SET lock_timeout=5000')
        elif url.startswith('sqlite:///'):
            self.db = sqlite3.connect(url[len('sqlite:///'):], isolation_level=None)
        else: raise ValueError('DATABASE_URL must be PostgreSQL or local sqlite:/// path')
        statements=[
            'CREATE TABLE IF NOT EXISTS usage (period TEXT PRIMARY KEY, used INTEGER NOT NULL)',
            'CREATE TABLE IF NOT EXISTS observations (id TEXT PRIMARY KEY, payload TEXT NOT NULL, observed_at TEXT NOT NULL)',
            'CREATE TABLE IF NOT EXISTS notifications (id TEXT PRIMARY KEY, state TEXT NOT NULL, payload TEXT NOT NULL, updated_at TEXT NOT NULL)',
            'CREATE TABLE IF NOT EXISTS scheduled_days (day TEXT PRIMARY KEY)',
            'CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, payload TEXT NOT NULL)'] if initialize else []
        if self.pg and initialize:self.execute('SELECT pg_advisory_lock(73541208)')
        try:
            for statement in statements:self.execute(statement)
        finally:
            if self.pg and initialize:self.execute('SELECT pg_advisory_unlock(73541208)')

    def execute(self, sql, args=()):
        return self.db.execute(sql.replace('?', '%s') if self.pg else sql, args)

    def lock(self):
        # Session-scoped: released on connection close, including process death.
        if self.pg:
            return bool(self.execute('SELECT pg_try_advisory_lock(73541209)').fetchone()[0])
        return True  # SQLite is local testing only; cloud requires PostgreSQL.

    def wait_for_scan_lock(self,timeout_seconds=120):
        """Queue behind a watchdog; never silently discard a scheduled scan."""
        if not self.pg:return True
        if not 1<=timeout_seconds<=120:raise ValueError('Bounded scan-lock timeout required')
        statement=self.execute('SHOW statement_timeout').fetchone()[0]
        lock=self.execute('SHOW lock_timeout').fetchone()[0]
        try:
            self.execute("SELECT set_config('statement_timeout',?,false)",(str((timeout_seconds+5)*1000),))
            self.execute("SELECT set_config('lock_timeout',?,false)",(str(timeout_seconds*1000),))
            self.execute('SELECT pg_advisory_lock(73541209)')
            return True
        except Exception:
            raise RuntimeError('Scheduled scan lock unavailable; no provider request attempted') from None
        finally:
            self.execute("SELECT set_config('statement_timeout',?,false)",(statement,))
            self.execute("SELECT set_config('lock_timeout',?,false)",(lock,))

    def reserve(self, day_limit, month_limit, now=None, total_limit=40):
        now = now or dt.datetime.now(dt.timezone.utc)
        # Legacy callers cannot bypass a protected listing allocation.
        has_settings=self.execute("SELECT to_regclass('settings')").fetchone()[0] if self.pg else self.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'").fetchone()
        if has_settings:
            from .discovery_allocation import saved
            if saved(self):raise BudgetExceeded('Legacy accounting cannot bypass the protected listing allocation')
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
          'CREATE TABLE IF NOT EXISTS email_errors (outbox_id TEXT PRIMARY KEY, payload TEXT NOT NULL)',
          'CREATE TABLE IF NOT EXISTS command_center_reports (outbox_id TEXT PRIMARY KEY REFERENCES outbox(id), payload TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, last_attempt_at TEXT, synced_at TEXT, error_type TEXT)',
          'CREATE TABLE IF NOT EXISTS api_attempts (request_key TEXT PRIMARY KEY, state TEXT NOT NULL, attempted_at TEXT NOT NULL, retry_after TEXT NOT NULL)',
          'CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, message TEXT NOT NULL, created_at TEXT NOT NULL)',
          'CREATE INDEX IF NOT EXISTS scan_tasks_pending ON scan_tasks(job_id,done,priority)',
          'CREATE INDEX IF NOT EXISTS deal_events_property ON deal_events(property_id,state)']
        with self.transaction():
            for statement in statements:self.execute(statement)
            self.execute('INSERT INTO schema_versions VALUES (2,?) ON CONFLICT(version) DO NOTHING',(self.now(),))
            self.execute('INSERT INTO schema_versions VALUES (3,?) ON CONFLICT(version) DO NOTHING',(self.now(),))

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

    def reconcile_provider_baseline(self,settings,now):
        """Seed the account-wide baseline and retain all overlapping legacy ledgers."""
        from .billing import billing_window,cycle_key
        start,end,anchor=billing_window(settings,now)
        if not settings.supplemental_requests:return {'state':'not_requested'}
        cutoff=dt.datetime.fromisoformat(settings.approval_expires_at.replace('Z','+00:00'))
        if start!=anchor or not anchor<=now<cutoff:return {'state':'expired'}
        with self.transaction():
            if self.pg:self.execute('SELECT pg_advisory_xact_lock(73541210)')
            daily=self.execute('SELECT COALESCE(SUM(used),0) FROM usage WHERE period>=? AND period<?',('day:'+start.date().isoformat(),'day:'+end.date().isoformat())).fetchone()[0]
            baseline=max(settings.billing_baseline,daily)
            self.execute('INSERT INTO usage VALUES (?,?) ON CONFLICT(period) DO UPDATE SET used=CASE WHEN usage.used<excluded.used THEN excluded.used ELSE usage.used END',(cycle_key(settings,now),baseline))
            body={'state':'reconciled','at':now.isoformat(),'provider_confirmed_used':settings.billing_baseline,'app_reserved_attempts_in_date_window':daily,'unreconciled_account_delta':settings.billing_baseline-daily,'cycle_key':cycle_key(settings,now),'additional_limit':settings.supplemental_requests,'expires_at':settings.approval_expires_at,'legacy_ledgers_preserved':True,'provider_calls':0}
            self.execute('INSERT INTO settings(name,payload) VALUES (?,?) ON CONFLICT(name) DO NOTHING',('budget_reconciliation:'+anchor.isoformat(),json.dumps(body)))
        return body

    def reserve_api(self,settings,now=None,*,tax_request=False,scope=None,purpose=None):
        from .billing import cycle_key, billing_window, future_listing_days
        now=now or dt.datetime.now(dt.timezone.utc)
        if settings.approval_mode!='monthly_budget' and (not settings.approval_id or settings.approved_requests<=0):raise BudgetExceeded('No approved live API allowance')
        start,end,anchor=billing_window(settings,now)
        if settings.approval_mode in {'billing_cycle','monthly_budget'} and now<anchor:
            raise BudgetExceeded('Approved subscription cycle has not started')
        if settings.supplemental_requests and (start!=anchor or now>=dt.datetime.fromisoformat(settings.approval_expires_at.replace('Z','+00:00'))):
            raise BudgetExceeded('Current-cycle supplemental approval expired; new provider verification and approval required')
        periods=[(now.strftime('day:%Y-%m-%d'),settings.per_day),
                 (cycle_key(settings,now),settings.per_month),
                 ('approval:'+settings.approval_id,settings.approved_requests)]
        with self.transaction():
            if self.pg:self.execute('SELECT pg_advisory_xact_lock(73541210)')
            from .discovery_allocation import reservation_periods
            allocation_periods=reservation_periods(self,settings,now,purpose,scope)
            if settings.approval_mode in {'billing_cycle','monthly_budget'}:
                # Seed the already consumed account usage once, without resetting
                # persisted app usage when an approval ID or process changes.
                baseline=settings.billing_baseline if start==anchor else 0
                self.execute('INSERT INTO usage VALUES (?,?) ON CONFLICT(period) DO UPDATE SET used=CASE WHEN usage.used<excluded.used THEN excluded.used ELSE usage.used END',
                             (cycle_key(settings,now),baseline))
                # The cycle is incremented once; changing an approval cannot reset it.
                periods=[periods[0],(cycle_key(settings,now),settings.request_ceiling)]
                if settings.supplemental_requests:
                    # This grant is independent of the baseline and cannot reset on approval renaming.
                    periods.append(('supplemental:'+anchor.isoformat(),settings.supplemental_requests))
                if tax_request:
                    future_days=future_listing_days(now,end)
                    if settings.request_ceiling-self.count_usage(cycle_key(settings,now))<=future_days*settings.listing_requests_per_day:
                        raise BudgetExceeded('Remaining API allowance reserved for future daily listing scans')
            elif settings.approval_mode=='rolling_32_days':
                periods=periods[:2]
                if self.rolling_usage(now)>=settings.approved_requests:
                    raise BudgetExceeded('Rolling 32-day approved API allowance reached')
            if scope:
                name,limit=scope
                if not isinstance(name,str) or len(name)>200 or not isinstance(limit,int) or limit<1:raise ValueError('Invalid durable request scope')
                periods.append(('request_scope:'+name,limit))
            periods.extend(allocation_periods)
            for key,limit in periods:
                self.execute('INSERT INTO usage VALUES (?,0) ON CONFLICT(period) DO NOTHING',(key,))
                if not self.execute('UPDATE usage SET used=used+1 WHERE period=? AND used<? RETURNING used',(key,limit)).fetchone():
                    raise BudgetExceeded('API allowance reached: '+key.split(':')[0])
        return self.budget_health(settings,now)

    def rolling_usage(self,now):
        start=(now-dt.timedelta(days=31)).strftime('day:%Y-%m-%d')
        end=now.strftime('day:%Y-%m-%d')
        return self.execute('SELECT COALESCE(SUM(used),0) FROM usage WHERE period>=? AND period<=?',(start,end)).fetchone()[0]

    def budget_health(self,settings,now=None,*,record_warning=True):
        from .billing import cycle_key,billing_window,future_listing_days
        now=now or dt.datetime.now(dt.timezone.utc)
        cycle,end,anchor=billing_window(settings,now)
        used=self.count_usage(cycle_key(settings,now))
        if settings.approval_mode in {'billing_cycle','monthly_budget'}:
            if cycle==anchor:used=max(used,settings.billing_baseline)
            approval_used=used
        else:
            approval_used=self.rolling_usage(now) if settings.approval_mode=='rolling_32_days' else self.count_usage('approval:'+settings.approval_id)
        remaining=max(0,settings.request_ceiling-used) if settings.approval_mode=='monthly_budget' else max(0,min(settings.per_month-used,settings.approved_requests-approval_used))
        supplemental_used=self.count_usage('supplemental:'+anchor.isoformat()) if settings.supplemental_requests else 0
        if settings.supplemental_requests:
            remaining=min(remaining,max(0,settings.supplemental_requests-supplemental_used))
            if cycle!=anchor or now>=dt.datetime.fromisoformat(settings.approval_expires_at.replace('Z','+00:00')):remaining=0
        if settings.approval_mode in {'billing_cycle','monthly_budget'} and now<anchor:remaining=0
        days=1+future_listing_days(now,end)
        warning=None
        if remaining<days*settings.listing_requests_per_day:warning='API allowance cannot cover daily listing pages for the remaining cycle; increase only after approval.'
        elif used>=settings.per_month*.85:warning='API billing-cycle allowance is at least 85% consumed.'
        if warning and record_warning:self.operation('budget:'+cycle.isoformat()+':'+str(remaining),warning)
        return dict(cycle_start=cycle.isoformat(),cycle_end=end.isoformat(),used=used,remaining=remaining,days_remaining=days,warning=warning,**({'supplemental_used':supplemental_used,'supplemental_limit':settings.supplemental_requests,'approval_expires_at':settings.approval_expires_at,'provider_baseline':settings.billing_baseline} if settings.supplemental_requests else {}))

    def scheduled_allowance(self,settings,now=None):
        now=now or dt.datetime.now(dt.timezone.utc)
        health=self.budget_health(settings,now)
        if not health['remaining']:return 0
        if settings.approval_mode=='rolling_32_days':
            start=(now-dt.timedelta(days=31)).strftime('day:%Y-%m-%d')
            today=now.strftime('day:%Y-%m-%d')
            prior_active=self.execute('SELECT COUNT(*) FROM usage WHERE period>=? AND period<? AND used>0',(start,today)).fetchone()[0]
            fair=max(1,health['remaining']-max(0,31-prior_active))
        else:
            fair=max(1,(health['remaining']+health['days_remaining']-1)//health['days_remaining'])
        today_remaining=max(0,settings.per_day-self.count_usage(now.strftime('day:%Y-%m-%d')))
        return min(settings.per_run,today_remaining,health['remaining'],fair)

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
        if not r:return None
        record=json.loads(r[0]);property_row=self.get_property(identity)
        if property_row and isinstance(record,dict):
            from .rental_intelligence import cached_estimate
            rent=cached_estimate(self,property_row['payload'],dt.datetime.fromisoformat(now.replace('Z','+00:00')))
            if rent:record={**record,'_rentcast_rent':rent}
        return record,r[1],r[2]

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
