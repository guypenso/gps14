"""One existing daily job: drain -> cached/selective verification -> shared outbox."""
import datetime as dt
from dataclasses import replace
from .realscout_config import RealScoutSettings
from .realscout_worker import run_once,monitor
from .realscout_enrichment import auto_link_cached,enrich,verify_active_status
from .realscout_delivery import prepare,send
from .telemetry import event


def daily(store,boundary,settings,day,*,rs_settings=None,clock=None,receiving_client=None,rentcast_client=None,opener=None):
    cfg=rs_settings or RealScoutSettings.from_env()
    if settings.discovery_source!='realscout':raise PermissionError('Daily source must explicitly be RealScout')
    if not cfg.daily_enabled:raise PermissionError('RealScout daily integration disabled')
    clock=clock or (lambda:dt.datetime.now(dt.timezone.utc))
    existing=store.execute('SELECT id,state,first_attempt_at FROM outbox WHERE job_id=?',('daily:'+day,)).fetchone()
    if existing and existing[1]=='sent':
        return dict(status='already_sent',outbox_id=existing[0],queue={'processed':0},enrichment={'requests':0})
    if existing and existing[2]:
        if existing[1]=='failed':return dict(status='rejected_requires_configuration_review',outbox_id=existing[0],queue={'processed':0},enrichment={'requests':0})
        result=send(store,boundary,settings,cfg,existing[0],clock=clock,opener=opener) if settings.email_enabled else 'prepared_not_sent'
        return dict(status=result,outbox_id=existing[0],queue={'processed':0},enrichment={'requests':0})
    queue={'processed':0}
    if cfg.ingest_enabled:
        queue=run_once(store,cfg,client=receiving_client,clock=clock)
    auto_link_cached(store,clock())
    financial={'requests':0,'processed':0,'reason':'selective_enrichment_disabled'}
    if cfg.enrichment_enabled:
        allowance=store.scheduled_allowance(settings,clock()) if settings.enabled else 0
        effective=replace(settings,per_run=max(1,allowance))
        from .collector import Client
        selective_client=rentcast_client or Client(store,effective,clock,cache_only=allowance<=0)
        selective_client.settings=effective
        if allowance<=0:selective_client.cache_only=True
        financial=enrich(store,boundary,effective,cfg,clock=clock,client=selective_client)
        status=verify_active_status(store,boundary,effective,cfg,clock=clock,client=selective_client)
        financial.update(requests=selective_client.count,status_checks=status['verified'])
        if allowance<=0:financial['reason']='cache_only_existing_allowance_unavailable'
    identity=prepare(store,boundary,settings,cfg,day,clock(),enrichment_stats=financial)
    result='prepared_not_sent'
    if settings.email_enabled:
        result=send(store,boundary,settings,cfg,identity,clock=clock,opener=opener)
        if result=='cancelled_stale':
            identity=prepare(store,boundary,settings,cfg,day,clock(),enrichment_stats=financial)
            result=send(store,boundary,settings,cfg,identity,clock=clock,opener=opener)
    event('realscout_daily_result',status=result,processed=queue['processed'],enrichment_requests=financial['requests'])
    return {'status':result,'outbox_id':identity,'queue':queue,'enrichment':financial}
