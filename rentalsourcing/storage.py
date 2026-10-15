import datetime as dt
import json
import sqlite3

class BudgetExceeded(RuntimeError): pass

class Store:
    def __init__(self, url):
        self.pg = url.startswith(('postgres://', 'postgresql://'))
        if self.pg:
            import psycopg
            self.db = psycopg.connect(url, autocommit=True, connect_timeout=15)
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
