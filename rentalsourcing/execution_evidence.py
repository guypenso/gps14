"""Bounded SELECT-only incident evidence. Never invokes health/recovery workers."""
import hashlib
import json
import datetime as dt
from urllib.parse import urlsplit
from .schedule import ISRAEL


def setting(store, name):
    row=store.execute('SELECT payload FROM settings WHERE name=?',(name,)).fetchone()
    return json.loads(row[0]) if row else {}


def safe_error(error):
    return {k:error[k] for k in ('category','http_status','retryable') if k in error} if isinstance(error,dict) else None


def snapshot(store,now,boundary=None):
    day=now.astimezone(ISRAEL).date().isoformat()
    run=setting(store,'discovery_run:'+day+':morning')
    metrics=setting(store,'discovery_metrics:'+day+':morning')
    result={'day':day,'as_of':now.isoformat(),'provider_calls':0,'qualification_changed':False,
        'scheduled_run':{k:run[k] for k in ('day','slot','started_at','finished_at','state') if k in run},
        'result':{k:v for k,v in run.get('result',{}).items() if k in
            ('status','scan','requests','error','workflow_status','outbox_id','unresolved_critical','price_queue')},
        'morning_metrics':{k:metrics[k] for k in ('started_at','finished_at','discovery_state','requests_consumed',
            'received_this_run','discovery_complete','discovery_partial','delivery_totals','penten_receipts','evening_inventory_receipt') if k in metrics}}
    row=store.execute('SELECT id,state,payload,provider_id,first_attempt_at,updated_at FROM outbox WHERE job_id=?',('daily:'+day,)).fetchone()
    if row:
        payload=json.loads(row[2]);body=payload.get('text','')
        result['email']={'outbox_id':row[0],'state':row[1],'subject':payload.get('subject'),
            'content_excerpt':body[:6500],'content_characters':len(body),'text_sha256':hashlib.sha256(body.encode()).hexdigest(),
            'provider_receipt_present':bool(row[3]),'provider_message_id':row[3],
            'first_attempt_at':row[4],'updated_at':row[5],
            'delivery_note':'Sent means provider acceptance; inbox delivery is not inferred without a delivery event.'}
    operations=[];incidents=[]
    for raw, in store.execute('SELECT payload FROM settings WHERE name LIKE ?',('sync_operation:%',)).fetchall():
        op=json.loads(raw)
        if op.get('state') not in ('pending','dead_letter'):continue
        operations.append({'component':op.get('component'),'state':op.get('state'),
            'endpoint':urlsplit(op.get('endpoint','')).path,'attempts':op.get('attempts'),
            'last_attempt_at':op.get('last_attempt_at'),'next_attempt_at':op.get('next_attempt_at'),
            'error':safe_error(op.get('error'))})
    for raw, in store.execute('SELECT payload FROM settings WHERE name LIKE ?',('health_incident:%',)).fetchall():
        item=json.loads(raw)
        if item.get('state')!='open':continue
        incidents.append({k:item.get(k) for k in ('component','state','opened_at','last_seen_at','manual_action')}|{'error':safe_error(item.get('error'))})
    prices=[json.loads(r[0]) for r in store.execute('SELECT payload FROM settings WHERE name LIKE ?',('price-observation:%',)).fetchall()]
    result['operations']={'pending_count':sum(op['state']=='pending' for op in operations),
        'dead_letter_count':sum(op['state']=='dead_letter' for op in operations),'items':operations[:20],
        'open_incident_count':len(incidents),'open_incidents':incidents[:20],
        'pending_price_observations':sum(not p.get('synced_at') for p in prices),
        'pending_report_count':store.execute('SELECT COUNT(*) FROM command_center_reports WHERE synced_at IS NULL').fetchone()[0]}
    audit=setting(store,'health_last_audit')
    current=audit.get('result',{}).get('current_inventory',{})
    result['saved_inventory_audit']={'at':audit.get('at'),**{k:current[k] for k in
        ('source_records','mapped','missing','missing_pending','missing_verified','rejected_at_destination',
         'tax_status_mismatch','pending_tax_status_mismatch','destination_review_statuses') if k in current}}
    reviews={name[len('penten_review:'):]:json.loads(raw) for name,raw in
        store.execute('SELECT name,payload FROM settings WHERE name LIKE ?',('penten_review:%',)).fetchall()}
    previous=(now.astimezone(ISRAEL).date()-dt.timedelta(days=1)).isoformat()
    prefix='multi-discovery:'+previous+':evening'
    prior=setting(store,prefix)
    cohort=[]
    for identity in prior.get('new_source_ids',[])[:50]:
        row=store.execute('SELECT payload,state,observed_at FROM properties WHERE id=?',(identity,)).fetchone()
        if not row:continue
        p=json.loads(row[0]);review=reviews.get(identity,{})
        cohort.append({'source_id':identity,'address':p.get('formattedAddress'),'source_state':row[1],
            'observed_at':row[2],'owner_status':review.get('status'),'reviewed_at':review.get('reviewed_at'),
            'reviewed_by':review.get('reviewed_by')})
    result['previous_evening']={k:prior[k] for k in ('run_key','started_at','finished_at','requests_consumed',
        'received_this_run','delivery_totals','penten_receipts') if k in prior}
    result['previous_evening_new_sources']=cohort
    if boundary is not None:
        from .review_inventory import select
        cards,counts=select(store,boundary,now,include_verified=True)
        unmapped=[card['source_id'] for card,p in cards if card['source_id'] not in reviews]
        result['stored_recovery']={'selection_counts':counts,'not_in_cached_destination_feedback':unmapped[:25],
            'unmapped_count':len(unmapped),'note':'Existing decisions and freshness still apply; no import is performed by this audit.'}
    return result


def compact_quality(value):
    if not isinstance(value,dict):return value
    rows=value.get('records',[])
    return {**{k:v for k,v in value.items() if k!='records'},'records_total':len(rows),
        'records':[ {k:r[k] for k in ('id','source_id','address','state','review_status','managed','observed_at',
            'reasons','insight_entries') if k in r} for r in rows[:50]],'evidence_note':'Full evidence remains in the stored source records.'}
