"""RealScout report adapter for the ORIGINAL durable daily Resend outbox."""
import datetime as dt
import hashlib
import json
from .digest import Digest,ReconciliationRequired
from .realscout_report import report,render_preview
from .realscout_store import decision


def prepare(store,boundary,settings,rs_settings,day,now,*,enrichment_stats=None):
    if not rs_settings.daily_enabled:raise PermissionError('RealScout daily reporting disabled')
    key='daily:'+day;outbox_id='daily-'+hashlib.sha256(key.encode()).hexdigest()
    # Shared scan lock serializes preparation with legacy collection and other workers.
    with store.transaction():
        if store.pg:store.execute('SELECT pg_advisory_xact_lock(73541209)')
        old=store.execute('SELECT id,state,first_attempt_at,event_ids FROM outbox WHERE job_id=?',(key,)).fetchone()
        if old and (old[2] or old[1] in {'sent','failed','uncertain','sending'}):return old[0]
        prior=store.execute("SELECT r.source_cutoff FROM realscout_reports r JOIN outbox o ON o.id=r.outbox_id WHERE o.state='sent' AND r.day<? ORDER BY r.day DESC LIMIT 1",(day,)).fetchone()
        snapshot=report(store,boundary,now,day=day,since=prior[0] if prior else None,
                        public_url=rs_settings.public_url)
        snapshot['budget']=store.budget_health(settings,now,record_warning=False)
        snapshot['enrichment_requests_this_run']=(enrichment_stats or {}).get('requests',0)
        body=render_preview(snapshot,preview=False)
        body.update({'from':settings.email_from,'to':[settings.email_to]})
        if old:
            for event_id in json.loads(old[3]):
                store.execute("UPDATE deal_events SET state='ready' WHERE id=? AND state='queued'",(event_id,))
        store.execute("""INSERT INTO outbox VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE
          SET state='pending',payload=excluded.payload,event_ids='[]',updated_at=excluded.updated_at
          WHERE outbox.first_attempt_at IS NULL AND outbox.state IN ('pending','cancelled')""",
          (outbox_id,key,'pending',json.dumps(body),'[]',None,None,now.isoformat()))
        store.execute('INSERT INTO realscout_reports VALUES (?,?,?,?,?) ON CONFLICT(outbox_id) DO UPDATE SET snapshot=excluded.snapshot,prepared_at=excluded.prepared_at,source_cutoff=excluded.source_cutoff',
                      (outbox_id,day,json.dumps(snapshot),now.isoformat(),snapshot['cutoff']))
        store.execute('DELETE FROM realscout_report_items WHERE outbox_id=?',(outbox_id,))
        for row in snapshot['rows']:
            store.execute('INSERT INTO realscout_report_items VALUES (?,?,?,?)',(outbox_id,row['id'],row['version'],row['state']))
    return outbox_id


def validate_frozen(store,boundary,outbox_id,now):
    meta=store.execute('SELECT snapshot FROM realscout_reports WHERE outbox_id=?',(outbox_id,)).fetchone()
    if not meta:return True
    data=json.loads(meta[0])
    for item in data['rows']:
        current=store.execute('SELECT version FROM realscout_candidates WHERE id=?',(item['id'],)).fetchone()
        if not current or current[0]!=item['version']:return False
        result=decision(store,item['id'],boundary,now)
        if item['state']=='Qualified' and (result['state']!='Qualified' or result.get('tax')!=item.get('tax')):return False
    return True


def send(store,boundary,settings,rs_settings,outbox_id,*,clock=None,opener=None):
    return Digest(store,boundary,settings,clock,opener,realscout_settings=rs_settings).send(outbox_id)
