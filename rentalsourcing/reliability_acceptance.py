"""Durable, bounded fourteen-window acceptance in the existing watchdog."""
import datetime as dt,os
from .schedule import ISRAEL
from .sync_runtime import get
KEY='reliability_acceptance:20261120'


def ensure(store,now):
    if get(store,KEY):return
    local=now.astimezone(ISRAEL)
    slots=[];day=local.date()
    while len(slots)<14:
        for hour in (8,20):
            at=dt.datetime.combine(day,dt.time(hour),tzinfo=ISRAEL)
            if at>local:slots.append({'day':day.isoformat(),'slot':'morning' if hour==8 else 'evening','at_israel':at.isoformat()})
        day+=dt.timedelta(days=1)
    store.save_setting(KEY,{'activated_at':now.isoformat(),'build_sha':os.getenv('RENDER_GIT_COMMIT'),
        'windows':slots[:14],'state':'observing','provider_calls':0,'email_sends':0})


def snapshot(store,now):
    saved=get(store,KEY,{})
    if not saved:return {'state':'not_activated','future_runs_verified':False}
    windows=[]
    for w in saved['windows']:
        prefix=w['day']+':'+w['slot'];at=dt.datetime.fromisoformat(w['at_israel'])
        due=now>=at+dt.timedelta(minutes=40)
        run=get(store,'discovery_run:'+prefix,{})
        evidence=get(store,'multi-discovery:'+prefix,{})
        outcomes=run.get('result',{});contract=outcomes.get('processing_outcome',{})
        receipt=bool(run.get('finished_at') and evidence.get('finished_at') and run.get('started_at') and evidence.get('started_at','')>=run['started_at'])
        stages=get(store,'window_stages:'+prefix,{})
        counts=evidence.get('delivery_totals',{});acks=evidence.get('penten_receipts',{})
        delivery_ok=counts.get('new_pending',0)+counts.get('new_qualified',0)<=counts.get('synced',0)<=sum(acks.get(k,0) for k in ('inserted','updated','rejected'))
        windows.append({**w,'due':due,'state':contract.get('state','missing' if due else 'awaiting_observation'),
            'receipt_valid':receipt,'expected_build':saved['build_sha'],'actual_build':run.get('build_sha'),
            'requests':evidence.get('requests_consumed',0),'records':evidence.get('received_this_run',0),
            'pages':len(evidence.get('pages',[])),'new_source_identities':counts.get('new_source_listings',0),
            'historical_recovery':counts.get('recovered_source_listings',0),'delivery_accounted_for':receipt and delivery_ok,
            'penten_acknowledgments':acks,'free_source':stages.get('free_source',{}),
            'price_queue':outcomes.get('price_queue',{}),'rent_requests':outcomes.get('rent_requests',0),
            'critical_failures':contract.get('critical_failures'),
            'pass':receipt and delivery_ok and contract.get('state') in ('fresh','free_source','paced') and not contract.get('exit_failure',True)})
    due=[w for w in windows if w['due']]
    return {'state':'passed' if len(due)==14 and all(w['pass'] for w in due) else 'needs_attention' if any(not w['pass'] for w in due) else 'observing',
        'activated_at':saved['activated_at'],'expected_windows':14,'elapsed_windows':len(due),
        'passed_windows':sum(w['pass'] for w in due),'missing_receipts':sum(not w['receipt_valid'] for w in due),
        'windows':windows,'future_runs_verified':False,'provider_calls':0,'email_sends':0,
        'acceptance_note':'Window evidence is necessary, not proof of full market coverage or independent provider-wide usage. Existing integrity and budget ledgers remain authoritative.'}
