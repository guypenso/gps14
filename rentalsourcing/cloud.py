"""Production entry point; no provider calls without an explicit approved allowance."""
import argparse
import datetime as dt
import json
import logging
import os
import uuid
from dataclasses import replace
from pathlib import Path
from .config import Settings
from .collector import Client
from .geography import load_boundary,POLICY
from .digest import Digest
from .engine import Engine
from .request import diagnose
from .schedule import scheduled_day,ISRAEL
from .storage import Store
from .core import EXCLUDED_ZIPS
from .database import validate_postgres_url
from .telemetry import event


def scheduled_job(store,day):
    # A partial inventory scan must keep its cursor across mornings. Tax-only
    # backlog is copied into the next fresh scan by Engine instead.
    old=store.execute("SELECT id FROM scan_jobs WHERE id LIKE ? AND collection_done=0 AND state IN ('running','partial','failed') ORDER BY day,id LIMIT 1",('daily:%',)).fetchone()
    return old[0] if old else 'daily:'+day


def _daily_discovery(store,boundary,settings,day,*,clock=None,opener=None):
    """Existing cron + collector + outbox. Caller holds the existing scan lock."""
    clock=clock or (lambda:dt.datetime.now(dt.timezone.utc))
    from .price_sync import safe_flush,safe_current_baselines
    safe_flush(store)
    digest=Digest(store,boundary,settings,clock,opener)
    old=store.execute('SELECT id,state FROM outbox WHERE job_id=?',('daily:'+day,)).fetchone()
    if old:
        result=digest.send(old[0]) if settings.email_enabled else 'prepared_not_sent'
        if result in {'sent','already_sent'}:safe_current_baselines(store,boundary)
        return dict(status=result,outbox_id=old[0],scan='existing_daily_outbox_no_rescan',requests=0)
    stats={};failure=None;client=None;feedback_ok=True
    # Human decisions are read before enrichment. Failed feedback keeps discovery
    # available while deferring paid tax work conservatively.
    from .command_center import configuration,review_configuration
    from .sync_runtime import classify,component,incident
    feedback_config=None;feedback_required=os.getenv('ENABLE_COMMAND_CENTER_REVIEW_FEEDBACK','false')=='true' or os.getenv('ENABLE_REVIEW_INVENTORY_SYNC','false')=='true'
    try:
        feedback_config=review_configuration() or configuration()
    except Exception as exc:
        feedback_ok=False;error=classify(exc,operation='review-feedback-configuration');component(store,'review_feedback','failed',now=clock(),pending=1,error=error.safe());incident(store,'review_feedback',error.safe(),now=clock())
    if feedback_config and (feedback_required or review_configuration()):
        from .review_feedback import refresh
        try:
            feedback=refresh(store,clock(),opener,feedback_config);event('penten_review_feedback',**feedback);feedback_ok=feedback.get('state')=='fresh'
        except Exception as exc:
            event('penten_review_feedback_failed',error_type=type(exc).__name__,http_status=getattr(exc,'http_status',getattr(exc,'code',None)))
            feedback_ok=False
    elif feedback_required:
        feedback_ok=False;error={'category':'review_configuration_missing','retryable':False};component(store,'review_feedback','failed',now=clock(),pending=1,error=error);incident(store,'review_feedback',error,now=clock())

    if not settings.enabled:stats=dict(discovery_state='skipped_disabled',partial=True)
    elif settings.approval_mode not in {'billing_cycle','one_time'}:stats=dict(discovery_state='skipped_billing_configuration',partial=True)
    else:
        # Reserve before EVERY wire attempt using existing atomic DB counters.
        effective=replace(settings,per_month=min(50,settings.per_month),retries=0,max_pages=1)
        allowance=store.scheduled_allowance(effective,clock())
        if allowance<=0:stats=dict(discovery_state='skipped_budget_exhausted',partial=True)
        else:
            effective=replace(effective,per_run=min(4,allowance))
            client=Client(store,effective,clock,opener)
            try:
                scan_job=scheduled_job(store,day)
                stats=Engine(store,boundary,client,effective,clock,on_discovery_complete=lambda:safe_flush(store),allow_paid_tax=feedback_ok).run(scan_job,day)
                stats['requests_consumed']=client.count
                if stats.get('sample_truncated'):stats['partial']=True
            except Exception as exc:
                failure=type(exc).__name__
                saved=store.execute('SELECT stats FROM scan_jobs WHERE id=?',(scan_job,)).fetchone()
                stats=json.loads(saved[0]) if saved else {}
                stats.update(partial=True,error_type=failure,error_phase='verification' if stats.get('discovery_state')=='fresh' else 'discovery')
                if stats.get('discovery_state') not in {'fresh','cached'}:stats['discovery_state']='failed'
                stats['requests_consumed']=client.count
    safe_flush(store)
    from .diagnostics import funnel,safe_read
    stats['funnel']=safe_read(funnel,store,boundary,clock());store.save_setting('health_funnel',stats['funnel'])
    message=digest.prepare(locals().get('scan_job','daily:'+day),day,stats)
    result=digest.send(message) if message and settings.email_enabled else 'prepared_not_sent'
    from .command_center import review_configuration
    try:
        config=review_configuration()
        if config and result in {'sent','already_sent'}:
            from .review_inventory import run
            run(store,boundary,clock(),execute=True,config=config,job_key='stored-review-inventory:'+day+':daily:v1')
            from .listing_enrichment import run as enrich_listings
            enrich_listings(store,config,job_key='listing-contact-enrichment:'+day+':daily:v1')
    except Exception as exc:event('stored_review_sync_deferred',error_type=type(exc).__name__)
    if result in {'sent','already_sent'}:safe_current_baselines(store,boundary)
    return dict(status=result,outbox_id=message,scan=stats.get('discovery_state','failed'),requests=client.count if client else 0,error=failure)


def daily_discovery(store,boundary,settings,day,*,clock=None,opener=None):
    from .reliability import begin,stage,finish,publish,snapshot
    from .sync_runtime import get
    clock=clock or (lambda:dt.datetime.now(dt.timezone.utc));begin(store,day,clock());stage(store,day,'scheduled_run','success',clock())
    store.save_setting('health_budget',store.budget_health(settings,clock(),record_warning=False))
    try:
        result=_daily_discovery(store,boundary,settings,day,clock=clock,opener=opener)
        scan=result.get('scan','unknown');last=store.execute('SELECT stats FROM scan_jobs ORDER BY updated_at DESC LIMIT 1').fetchone();stats=json.loads(last[0]) if last else {}
        stage(store,day,'discovery','failed' if scan=='failed' else 'partial' if scan.startswith('skipped_') else 'success',clock(),processed=stats.get('received_this_run',stats.get('received',0)),expected=scan.startswith('skipped_'),coverage=scan)
        for name in ('listing_processing','eligibility_filtering','property_storage','price_history'):
            stage(store,day,name,'failed' if result.get('error') else 'success',clock(),processed=stats.get('received_this_run',stats.get('received',0)))
        tax=store.execute("SELECT COUNT(*) FROM properties WHERE state='Pending Verification'").fetchone()[0]
        stage(store,day,'tax_verification','partial' if tax else 'success',clock(),pending=tax,expected=True,coverage='Pending evidence remains unverified; API approval limits preserved')
        stage(store,day,'email_delivery','success' if result.get('status') in ('sent','already_sent') else 'failed',clock(),processed=1 if result.get('status') in ('sent','already_sent') else 0,pending=0 if result.get('status') in ('sent','already_sent') else 1)
        status=finish(store,day,clock());result['workflow_status']=status;result['email_status']=result.get('status')
        current=snapshot(store,clock());result['unresolved_critical']=current['counts']['pending_reports']+current['counts']['pending_operations']+current['counts']['dead_letters']+len([i for i in current['incidents'] if i['state']=='open'])
        try:publish(store,clock(),opener=opener)
        except Exception as exc:event('operational_health_deferred',error_type=type(exc).__name__)
        result['workflow_status']=finish(store,day,clock())
        current=snapshot(store,clock());result['unresolved_critical']=current['counts']['pending_reports']+current['counts']['pending_operations']+current['counts']['dead_letters']+current['counts']['pending_price_observations']+current['counts']['pending_inventory_jobs']+len([i for i in current['incidents'] if i['state']=='open'])
        return result
    except Exception as exc:
        from .sync_runtime import classify
        error=classify(exc,operation='daily-workflow');stage(store,day,'email_delivery' if 'Resend' in type(exc).__name__ else 'daily_workflow','failed',clock(),error=error.safe());finish(store,day,clock())
        try:publish(store,clock(),opener=opener)
        except Exception:pass
        raise


def main(argv=None):
    parser=argparse.ArgumentParser(description='RentalSourcing daily production collector')
    group=parser.add_mutually_exclusive_group()
    group.add_argument('--check',action='store_true',help='Check config, boundary and read-only sync authentication; no provider calls')
    group.add_argument('--scheduled',action='store_true',help='Explicit collector run in the Israel 08:00 window; requires API approval')
    group.add_argument('--scheduled-email',action='store_true',help='08:00 Israel stored-data email only; never collect')
    group.add_argument('--prepare-email',action='store_true',help='Prepare a saved-data email without sending')
    group.add_argument('--prepare-test-email',action='store_true',help='Prepare a delivery test without sending or consuming property events')
    group.add_argument('--inspect-email',metavar='OUTBOX_ID',help='Inspect sanitized stored request and rejection; no send')
    group.add_argument('--replace-rejected-test',metavar='OUTBOX_ID',help='Prepare replacement only for confirmed rejected delivery test')
    group.add_argument('--email-status',metavar='OUTBOX_ID',help='Read Resend delivery status; no send')
    parser.add_argument('--email-day',help='Preparation target Israeli date YYYY-MM-DD')
    group.add_argument('--resume',metavar='JOB_ID',help='Resume persisted scan with the same cursor/tasks')
    group.add_argument('--status',action='store_true',help='Show persisted scans, pending verification and budget')
    group.add_argument('--migrate',action='store_true',help='Apply additive database migrations only')
    group.add_argument('--retry-email',metavar='OUTBOX_ID',help='Retry frozen daily digest within safe idempotency window')
    group.add_argument('--reconcile-email',metavar='OUTBOX_ID',help='Mark message accepted after checking Resend manually')
    parser.add_argument('--provider-message-id',help='Actual accepted message id copied from Resend')
    args=parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,format='%(message)s')
    settings=Settings.from_env()
    if args.check:
        from .tax_backlog import review_deferrals
        event('tax_review_deferrals',count=len(review_deferrals()))
        from .command_center import configuration
        configuration()
        from .command_center import review_configuration
        review_configuration()
        exists=Path(settings.boundary_file).is_file()
        if exists:
            boundary=load_boundary(settings.boundary_file)
            from .request import listing_parameters
            event('geographic_policy',policy=POLICY,municipalities=['Pittsburgh','Bellevue Borough'],zip_15212_neighborhoods=['Brighton Heights','Spring Hill-City View'],excluded_neighborhoods=['Sheraden','Hazelwood','Mount Washington','Perry South','Elliott','Carrick'],query=listing_parameters(boundary))
        from .sync_probe import inspect
        event('sync_authentication_probe',**inspect())
        if settings.enabled and os.getenv('DATABASE_URL'):
            observer=None
            try:
                observer=Store(validate_postgres_url(os.getenv('DATABASE_URL')),initialize=False)
                observer.save_setting('health_budget',observer.budget_health(settings,record_warning=False))
            except Exception as exc:event('budget_telemetry_unavailable',error_type=type(exc).__name__)
            finally:
                if observer:observer.close()
        event('preflight',boundary_valid=exists,api_approved=settings.approved_requests>0 and bool(settings.approval_id),
              configured=settings.public(),database_configured=bool(os.getenv('DATABASE_URL')),
              rentcast_secret_configured=bool(os.getenv('RENTCAST_API_KEY')),resend_secret_configured=bool(os.getenv('RESEND_API_KEY')))
        if not exists:raise ValueError('Official city boundary missing; run scripts/fetch_boundary.py in the deployment environment')
        return
    day=dt.datetime.now(dt.timezone.utc).astimezone(ISRAEL).date().isoformat()
    if args.scheduled or args.scheduled_email:
        day=scheduled_day()
        if day is None:event('outside_israel_schedule');return
    administrative=args.scheduled or args.status or args.migrate or args.retry_email or args.reconcile_email or args.prepare_email or args.scheduled_email or args.email_status or args.prepare_test_email or args.inspect_email or args.replace_rejected_test
    if not administrative and not settings.enabled:event('collector_disabled');return
    url=os.getenv('DATABASE_URL','')
    validate_postgres_url(url)
    store=Store(url)
    try:
        if not store.lock():
            if args.migrate:raise RuntimeError('Database is busy; migrations did not run. Retry deployment after the active job completes.')
            event('scan_already_running');return
        store.migrate()
        if args.scheduled:
            from .reliability import begin
            begin(store,day,dt.datetime.now(dt.timezone.utc))
        boundary=load_boundary(settings.boundary_file)
        if args.migrate:event('migrations_complete');return
        from .tax_backlog import reconcile
        event('tax_cache_recovery',**reconcile(store,settings,dt.datetime.now(dt.timezone.utc)))
        from .criteria import reevaluate
        changed=reevaluate(store,boundary,dt.datetime.now(dt.timezone.utc))
        event('stored_criteria_rechecked',changed=changed)
        event('neighborhood_exclusions',**{reason:store.execute('SELECT COUNT(*) FROM properties WHERE reasons LIKE ?',('%'+reason+'%',)).fetchone()[0] for reason in ['hazelwood_neighborhood','mount_washington_neighborhood','perry_south_neighborhood','elliott_neighborhood','carrick_neighborhood','condominium']})
        from .diagnostics import funnel,sync_health,safe_read
        event('pipeline_funnel',**safe_read(funnel,store,boundary,dt.datetime.now(dt.timezone.utc)))
        event('command_center_health',**safe_read(sync_health,store))
        event('allowance_health',**store.budget_health(settings,record_warning=False))
        if args.status:
            event('status',budget=store.budget_health(settings),
              jobs=store.execute('SELECT id,state,offset_value,collection_done FROM scan_jobs ORDER BY updated_at DESC LIMIT 10').fetchall(),
              states=store.execute('SELECT state,COUNT(*) FROM properties GROUP BY state').fetchall(),
              outbox=store.execute('SELECT id,state,provider_id FROM outbox ORDER BY updated_at DESC LIMIT 10').fetchall(),
              operations=store.operations());return
        digest=Digest(store,boundary,settings)
        if args.inspect_email:event('email_request_inspected',**digest.inspect_request(args.inspect_email));return
        if args.replace_rejected_test:event('test_email_replaced',outbox_id=digest.replace_rejected_test(args.replace_rejected_test));return
        if args.prepare_test_email:event('test_email_prepared',outbox_id=digest.prepare_test());return
        if args.email_status:event('email_delivery',**digest.delivery_status(args.email_status));return
        if args.prepare_email or args.scheduled_email:
            target=dt.date.fromisoformat(args.email_day).isoformat() if args.email_day else day
            if args.scheduled_email and args.email_day:raise ValueError('Scheduled email date must be the current Israeli date')
            message=digest.prepare_stored(target)
            event('stored_email_prepared',outbox_id=message,day=target,source='saved_data_only')
            if args.scheduled_email:
                result=digest.send(message)
                if result in {'sent','already_sent'}:store.claim_scheduled_day('israel:'+target)
                event('stored_email_result',result=result,outbox_id=message)
            return
        if args.retry_email:digest.send(args.retry_email);return
        if args.reconcile_email:digest.reconcile(args.reconcile_email,args.provider_message_id);return
        if args.scheduled:
            result=daily_discovery(store,boundary,settings,day);event('daily_discovery_result',**result)
            if result.get('unresolved_critical') or result.get('workflow_status')=='failed':raise SystemExit(2)
            return
        store.save_setting('runtime',settings.public())
        store.save_setting('recipient',{'email_to':settings.email_to,'email_from':settings.email_from})
        store.save_setting('criteria',dict(city='Pittsburgh',additional_municipality='Bellevue Borough',zip_15212_neighborhoods=['Brighton Heights','Spring Hill-City View'],excluded_neighborhoods=['Sheraden','Hazelwood','Mount Washington','Perry South','Elliott','Carrick'],excluded_property_types=['Townhouse','Townhome','Rowhouse','Condominium'],state='PA',bedrooms_min=3,bathrooms_min=1,sqft_min=1000,price_max=200000,excluded_zip=sorted(EXCLUDED_ZIPS),tax_add=100,tax_strict_max=2000))
        job_id=args.resume or (scheduled_job(store,day) if args.scheduled else 'manual:'+day+':'+str(uuid.uuid4()))
        if args.resume:
            job=store.execute('SELECT day FROM scan_jobs WHERE id=?',(job_id,)).fetchone()
            if not job:raise ValueError('Unknown scan to resume')
            day=job[0]
        health=store.budget_health(settings)
        if args.scheduled:
            # Keep allowance available for later days instead of exhausting it early.
            allowance=store.scheduled_allowance(settings)
            if allowance<=0:event('scheduled_budget_exhausted');return
            settings=replace(settings,per_run=allowance)
        stats={};failure=None
        try:stats=Engine(store,boundary,Client(store,settings),settings).run(job_id,day)
        except Exception as exc:
            failure=exc;stats={'partial':True,'error_type':type(exc).__name__};event('scan_failed',**diagnose(exc))
        # An operational warning digest is still prepared when collection fails.
        message=digest.prepare(job_id,day,stats)
        if message:digest.send(message)
        event('scan_result',job_id=job_id,**stats)
        if failure:raise failure
    finally:store.close()

if __name__=='__main__':
    try:main()
    except Exception as exc:
        event('command_failed',**diagnose(exc));raise SystemExit(1)
