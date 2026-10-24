"""Additive unified evidence schema. No legacy row/history/counter is replaced."""

def migrate(store):
    statements=[
      'CREATE TABLE IF NOT EXISTS discovery_export_events (source TEXT NOT NULL,event TEXT NOT NULL,body_hash TEXT NOT NULL,processed_at TEXT NOT NULL,PRIMARY KEY(source,event))',
      'CREATE TABLE IF NOT EXISTS discovery_source_metrics (source TEXT PRIMARY KEY,attempts INTEGER NOT NULL,duplicates INTEGER NOT NULL)',
      'CREATE TABLE IF NOT EXISTS discovery_schema (version INTEGER PRIMARY KEY)',
      'CREATE TABLE IF NOT EXISTS discovery_sources (name TEXT PRIMARY KEY,payload TEXT NOT NULL,last_success TEXT,last_attempt TEXT,last_error TEXT,cursor TEXT)',
      'CREATE TABLE IF NOT EXISTS discovery_entities (id TEXT PRIMARY KEY,address_key TEXT NOT NULL UNIQUE,address TEXT NOT NULL,first_seen TEXT NOT NULL,last_changed TEXT NOT NULL,version INTEGER NOT NULL)',
      'CREATE TABLE IF NOT EXISTS discovery_records (source TEXT NOT NULL,record_id TEXT NOT NULL,entity_id TEXT NOT NULL,payload TEXT NOT NULL,observed_at TEXT NOT NULL,PRIMARY KEY(source,record_id))',
      'CREATE TABLE IF NOT EXISTS discovery_observations (id TEXT PRIMARY KEY,source TEXT NOT NULL,record_id TEXT NOT NULL,entity_id TEXT NOT NULL,payload TEXT NOT NULL,observed_at TEXT NOT NULL,processed_at TEXT NOT NULL,kind TEXT NOT NULL)',
      'CREATE TABLE IF NOT EXISTS discovery_reports (outbox_id TEXT PRIMARY KEY,day TEXT NOT NULL UNIQUE,snapshot TEXT NOT NULL,cutoff TEXT NOT NULL)',
      'CREATE TABLE IF NOT EXISTS discovery_api_ledger (id TEXT PRIMARY KEY,cycle TEXT NOT NULL,purpose TEXT NOT NULL,request_hash TEXT NOT NULL,attempted_at TEXT NOT NULL,outcome TEXT NOT NULL)',
      'CREATE TABLE IF NOT EXISTS discovery_benchmarks (id TEXT PRIMARY KEY,payload TEXT NOT NULL,created_at TEXT NOT NULL)',
      'CREATE INDEX IF NOT EXISTS discovery_obs_entity ON discovery_observations(entity_id,observed_at)',
      'CREATE INDEX IF NOT EXISTS discovery_api_cycle ON discovery_api_ledger(cycle,purpose)',
    ]
    with store.transaction():
        for sql in statements:store.execute(sql)
        store.execute('INSERT INTO discovery_schema VALUES (1) ON CONFLICT(version) DO NOTHING')
