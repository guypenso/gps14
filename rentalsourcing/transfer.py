"""Move persisted application state to an EMPTY database; never calls providers.

Run with SOURCE_DATABASE_URL and DATABASE_URL in private environment variables:
python -m rentalsourcing.transfer
Keeps usage and outbox history so migration cannot reset allowances or sends.
"""
import json
import os
from .storage import Store

TABLES=('usage','observations','notifications','scheduled_days','runs','settings',
        'properties','price_history','tax_history','response_cache','scan_jobs',
        'scan_tasks','deal_events','outbox','operations','email_errors','api_attempts',
        'realscout_messages','realscout_candidates','realscout_observations','realscout_links',
        'realscout_inbound_jobs','realscout_evidence','realscout_status_evidence','realscout_reports',
        'realscout_report_items','realscout_health','realscout_dkim_keys','realscout_auth_evidence','discovery_sources','discovery_entities','discovery_records','discovery_observations','discovery_reports','discovery_api_ledger','discovery_benchmarks','discovery_export_events','discovery_source_metrics')


def transfer(source,target):
    if source.db is target.db:raise ValueError('Source and target must differ')
    if not source.lock() or not target.lock():
        raise RuntimeError('A scanner is active; transfer requires both databases idle')
    target.migrate()
    with source.transaction():
        if source.pg:source.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        with target.transaction():
            # Never overwrite running state, history, counters or an earlier import.
            if any(target.execute('SELECT 1 FROM '+table+' LIMIT 1').fetchone() for table in TABLES):
                raise ValueError('Target must be empty; existing application state was preserved')
            counts={}
            for table in TABLES:
                cursor=source.execute('SELECT * FROM '+table)
                columns=[c[0] for c in cursor.description]
                expected=[c[0] for c in target.execute('SELECT * FROM '+table+' LIMIT 0').description]
                if columns!=expected:raise ValueError('Schema differs for '+table)
                rows=cursor.fetchall()
                for row in rows:
                    target.execute('INSERT INTO '+table+' VALUES ('+','.join('?' for _ in columns)+')',row)
                counts[table]=len(rows)
    return counts


def main():
    source_url=os.environ['SOURCE_DATABASE_URL'];target_url=os.environ['DATABASE_URL']
    if source_url==target_url:raise ValueError('Source and target must differ')
    if not all(url.startswith(('postgres://','postgresql://')) for url in (source_url,target_url)):
        raise ValueError('Production transfer requires PostgreSQL')
    source=Store(source_url,initialize=False)
    try:
        target=Store(target_url)
        try:print(json.dumps({'copied_rows':transfer(source,target),'provider_requests':0,'emails_sent':0}))
        finally:target.close()
    finally:source.close()


if __name__=='__main__':main()
