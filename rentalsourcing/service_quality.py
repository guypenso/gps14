"""Seven-day persisted execution evidence for the existing internal watchdog.

No future acceptance claims, external calls, notifications or budget changes.
"""
import datetime as dt
import json
from .schedule import ISRAEL


def snapshot(store,now,days=7):
    if not 1<=days<=7:raise ValueError('Bounded observation window required')
    local=now.astimezone(ISRAEL);windows=[]
    def saved(key):
        row=store.execute('SELECT payload FROM settings WHERE name=?',(key,)).fetchone()
        return json.loads(row[0]) if row else {}
    for ago in range(days-1,-1,-1):
        day=(local.date()-dt.timedelta(days=ago)).isoformat()
        for slot,hour in (('morning',8),('evening',20)):
            scheduled=dt.datetime.combine(dt.date.fromisoformat(day),dt.time(hour),tzinfo=ISRAEL)
            if local<scheduled+dt.timedelta(minutes=40):continue
            run=saved('discovery_run:'+day+':'+slot)
            discovery=saved('multi-discovery:'+day+':'+slot)
            deliveries=discovery.get('delivery_totals') or {};acks=discovery.get('penten_receipts') or {}
            current=bool(run.get('started_at') and discovery.get('started_at','')>=run['started_at'])
            complete=bool(run.get('finished_at') and discovery.get('finished_at') and current)
            acknowledged=sum(acks.get(k,0) for k in ('inserted','updated','rejected'))
            needs_ack=deliveries.get('synced',0)
            delivery_ok=bool(needs_ack<=acknowledged and deliveries.get('new_pending',0)+deliveries.get('new_qualified',0)<=needs_ack)
            fresh=bool(complete and discovery.get('fresh_request_made') and discovery.get('requests_consumed',0)>0 and discovery.get('pages'))
            windows.append({'day':day,'slot':slot,'at_israel':scheduled.isoformat(),
                'execution':run.get('state','missing'),'receipt_current':current,'receipts_complete':complete,
                'discovery':discovery.get('discovery_state','missing'),'fresh_provider_discovery':fresh,
                'requests':discovery.get('requests_consumed',0),'pages':len(discovery.get('pages',[])),
                'records':discovery.get('received_this_run',0),'new_source_identities':deliveries.get('new_source_listings',0),
                'historical_recovery':deliveries.get('recovered_source_listings',0),
                'penten_acknowledgments':acks,'delivery_accounted_for':complete and delivery_ok,
                'partial_provider_coverage':discovery.get('discovery_partial'),
                'workflow_complete':complete and run.get('state')=='success'})
    return {'as_of':now.isoformat(),'days':days,'expected_elapsed_windows':len(windows),
        'execution_records':sum(w['execution']!='missing' for w in windows),
        'current_complete_receipt_pairs':sum(w['receipts_complete'] for w in windows),
        'fresh_discovery_windows':sum(w['fresh_provider_discovery'] for w in windows),
        'workflow_complete_windows':sum(w['workflow_complete'] for w in windows),
        'unaccounted_delivery_windows':sum(not w['delivery_accounted_for'] for w in windows),
        'windows':windows,'future_runs_verified':False,'provider_calls':0}
