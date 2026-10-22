"""Idempotent additive migrations; never reset or remove existing application data."""
import datetime as dt


def columns(store,table):
    if store.pg:
        return {r[0] for r in store.execute('SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=?',(table,)).fetchall()}
    return {r[1] for r in store.execute('PRAGMA table_info('+table+')').fetchall()}


def upgrade(store):
    additions={
      'realscout_messages':{'processed_at':'TEXT'},
      'realscout_inbound_jobs':{'next_attempt_at':"TEXT DEFAULT '1970-01-01T00:00:00+00:00'",
          'lease_until':'TEXT','lease_token':'TEXT','error_category':'TEXT','updated_at':'TEXT','profile':'TEXT','recipient_alias':'TEXT'},
    }
    tables=[
      'CREATE TABLE IF NOT EXISTS realscout_dkim_keys (name TEXT PRIMARY KEY,payload TEXT,fetched_at TEXT NOT NULL,expires_at TEXT NOT NULL)',
      'CREATE TABLE IF NOT EXISTS realscout_auth_evidence (email_id TEXT PRIMARY KEY,raw_sha256 TEXT NOT NULL,signing_domain TEXT NOT NULL,selector TEXT NOT NULL,verified_at TEXT NOT NULL,method TEXT NOT NULL,message_key TEXT NOT NULL)',
      'CREATE TABLE IF NOT EXISTS realscout_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)',
      '''CREATE TABLE IF NOT EXISTS realscout_evidence (candidate_id TEXT PRIMARY KEY,property_id TEXT NOT NULL,
           payload TEXT NOT NULL,source TEXT NOT NULL,fetched_at TEXT NOT NULL,expires_at TEXT NOT NULL)''',
      '''CREATE TABLE IF NOT EXISTS realscout_reports (outbox_id TEXT PRIMARY KEY,day TEXT NOT NULL UNIQUE,
           snapshot TEXT NOT NULL,prepared_at TEXT NOT NULL,source_cutoff TEXT NOT NULL)''',
      '''CREATE TABLE IF NOT EXISTS realscout_report_items (outbox_id TEXT NOT NULL,candidate_id TEXT NOT NULL,
           version INTEGER NOT NULL,state TEXT NOT NULL,PRIMARY KEY(outbox_id,candidate_id))''',
      '''CREATE TABLE IF NOT EXISTS realscout_health (name TEXT PRIMARY KEY,payload TEXT NOT NULL,updated_at TEXT NOT NULL)''',
      '''CREATE TABLE IF NOT EXISTS realscout_status_evidence (candidate_id TEXT PRIMARY KEY,payload TEXT NOT NULL,source TEXT NOT NULL,fetched_at TEXT NOT NULL,expires_at TEXT NOT NULL)''',
      'CREATE INDEX IF NOT EXISTS realscout_job_queue ON realscout_inbound_jobs(state,next_attempt_at,lease_until)',
      'CREATE INDEX IF NOT EXISTS realscout_observed_candidate ON realscout_observations(candidate_id,sent_at)',
    ]
    # Caller owns the transaction; a failed upgrade rolls back atomically.
    for table,fields in additions.items():
        present=columns(store,table)
        for name,definition in fields.items():
            if name not in present:store.execute('ALTER TABLE '+table+' ADD COLUMN '+name+' '+definition)
    for sql in tables:store.execute(sql)
    store.execute('UPDATE realscout_messages SET processed_at=received_at WHERE processed_at IS NULL')
    store.execute('INSERT INTO realscout_migrations VALUES (?,?) ON CONFLICT(version) DO NOTHING',
                  (1,dt.datetime.now(dt.timezone.utc).isoformat()))

    store.execute('INSERT INTO realscout_migrations VALUES (?,?) ON CONFLICT(version) DO NOTHING',(2,dt.datetime.now(dt.timezone.utc).isoformat()))
