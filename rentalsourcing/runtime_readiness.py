"""SELECT-only runtime evidence; no provider, email, migration or recovery calls."""
import datetime as dt
import json
import os

from .monthly_allocation import snapshot as allocation
from .partition_discovery import snapshot as coverage
from .rental_intelligence import job_summary
from .schedule import ISRAEL


def snapshot(store, settings, now):
    def saved(name):
        row=store.execute('SELECT payload FROM settings WHERE name=?',(name,)).fetchone()
        return json.loads(row[0]) if row else {}
    day=now.astimezone(ISRAEL).date().isoformat()
    ledger=[{'period':period,'used':used} for period,used in
            store.execute('SELECT period,used FROM usage ORDER BY period').fetchall()]
    dates=[]
    for delta in range(3):
        date=now.astimezone(ISRAEL).date()+dt.timedelta(days=delta)
        for hour in (8,20):
            future=dt.datetime.combine(date,dt.time(hour),tzinfo=ISRAEL).astimezone(dt.timezone.utc)
            if future<=now:continue
            from .monthly_allocation import available
            cap=min(settings.per_run,settings.per_day-store.count_usage(future.strftime('day:%Y-%m-%d')),
                    available(store,settings,future,'listing'))
            dates.append({'at_israel':future.astimezone(ISRAEL).isoformat(),
                          'listing_limit_upper_bound':max(0,cap),
                          'note':'Assumes no intervening requests; atomic reservation is checked at execution.'})
    tax=saved('public_tax_batch:2026')
    estimates=tax.get('estimates',{})
    current=saved('health_last_audit').get('result',{}).get('current_inventory',{})
    slots={}
    for slot in ('morning','evening'):
        receipt=saved('discovery_run:'+day+':'+slot)
        slots[slot]={k:receipt[k] for k in ('day','slot','started_at','finished_at','state','result') if k in receipt}
    scheduler=saved('offmarket_scheduler')
    pre_report=saved('listing_pre_report_receipt')
    return {'as_of':now.isoformat(),'build_sha':os.getenv('RENDER_GIT_COMMIT'),
            'settings':settings.public(),'provider_calls':0,'email_sends':0,'read_only':True,
            'budget':store.budget_health(settings,now,record_warning=False),
            'monthly_allocation':allocation(store,settings,now),'ledger':ledger,
            'next_slots':dates[:4],'coverage':coverage(store,now),'scheduled_slots':slots,
            'source_records':store.execute('SELECT COUNT(*) FROM properties').fetchone()[0],
            'source_states':dict(store.execute('SELECT state,COUNT(*) FROM properties GROUP BY state').fetchall()),
            'rental_estimates':{'saved':store.execute('SELECT COUNT(*) FROM settings WHERE name LIKE ?',('rentcast-long-term:%',)).fetchone()[0],
                                'jobs':job_summary(store),
                                'enabled':os.getenv('ENABLE_RENT_ESTIMATES')=='true'},
            'tax_estimates':{'estimated':sum(e.get('state')=='estimated' for e in estimates.values()),
                             'unknown':sum(e.get('state')=='unknown' for e in estimates.values()),
                             'matched':sum(e.get('match_status')=='unique_parcel' for e in estimates.values()),
                             'source_manifest':tax.get('manifest')},
            'realscout':{'scheduler':{k:scheduler[k] for k in ('attempted_at','next_at','state','error_type') if k in scheduler},
                         'pre_report':{'at':pre_report.get('at'),'day':pre_report.get('day'),
                                       'state':pre_report.get('result',{}).get('state')}},
            'destination_inventory':{k:current[k] for k in ('source_records','mapped','missing','missing_pending',
                'missing_verified','rejected_at_destination','tax_status_mismatch','pending_tax_status_mismatch',
                'destination_review_statuses') if k in current}}
