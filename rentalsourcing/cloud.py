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
from .geography import load_boundary
from .digest import Digest
from .engine import Engine
from .request import diagnose
from .schedule import scheduled_day,ISRAEL
from .storage import Store
from .database import validate_postgres_url
from .telemetry import event


def scheduled_job(store,day):
    # A partial inventory scan must keep its cursor across mornings. Tax-only
    # backlog is copied into the next fresh scan by Engine instead.
    old=store.execute("SELECT id FROM scan_jobs WHERE id LIKE ? AND collection_done=0 AND state IN ('running','partial','failed') ORDER BY day,id LIMIT 1",('daily:%',)).fetchone()
    return old[0] if old else 'daily:'+day


def main(argv=None):
    parser=argparse.ArgumentParser(description='RentalSourcing daily production collector')
    group=parser.add_mutually_exclusive_group()
    group.add_argument('--check',action='store_true',help='Check config and boundary offline; no provider calls')
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
    group.add_argument('--realscout-worker-once',action='store_true',help='Drain authorized inbound queue; never send')
    group.add_argument('--realscout-requeue',metavar='EMAIL_ID',help='Explicitly requeue a quarantined/retry receiving job; never resets API allowance or sends')
    group.add_argument('--realscout-status',action='store_true',help='Read-only inbound queue health; no provider calls')
    group.add_argument('--migrate',action='store_true',help='Apply additive database migrations only')
    group.add_argument('--retry-email',metavar='OUTBOX_ID',help='Retry frozen daily digest within safe idempotency window')
    group.add_argument('--reconcile-email',metavar='OUTBOX_ID',help='Mark message accepted after checking Resend manually')
    parser.add_argument('--provider-message-id',help='Actual accepted message id copied from Resend')
    args=parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,format='%(message)s')
    settings=Settings.from_env()
    from .realscout_config import RealScoutSettings
    rs_settings=RealScoutSettings.from_env()
    from .discovery.config import DiscoverySettings
    discovery_settings=DiscoverySettings.from_env()
    if args.check:
        exists=Path(settings.boundary_file).is_file()
        if exists:load_boundary(settings.boundary_file)
        event('preflight',boundary_valid=exists,api_approved=settings.approved_requests>0 and bool(settings.approval_id),
              configured=settings.public(),database_configured=bool(os.getenv('DATABASE_URL')),
              rentcast_secret_configured=bool(os.getenv('RENTCAST_API_KEY')),resend_secret_configured=bool(os.getenv('RESEND_API_KEY')))
        if not exists:raise ValueError('Official city boundary missing; run scripts/fetch_boundary.py in the deployment environment')
        return
    day=dt.datetime.now(dt.timezone.utc).astimezone(ISRAEL).date().isoformat()
    if args.scheduled or args.scheduled_email:
        day=scheduled_day()
        if day is None:event('outside_israel_schedule');return
    if settings.discovery_source=='realscout' and args.scheduled:
        if not rs_settings.daily_enabled:event('realscout_daily_disabled');return
    administrative=args.realscout_requeue or args.realscout_worker_once or args.realscout_status or args.status or args.migrate or args.retry_email or args.reconcile_email or args.prepare_email or args.scheduled_email or args.email_status or args.prepare_test_email or args.inspect_email or args.replace_rejected_test
    if not administrative and not settings.enabled:event('collector_disabled');return
    url=os.getenv('DATABASE_URL','')
    validate_postgres_url(url)
    if args.realscout_status:
        store=Store(url,initialize=False)
        try:
            store.execute('SET default_transaction_read_only = on')
            from .realscout_worker import monitor
            event('realscout_status',**monitor(store,dt.datetime.now(dt.timezone.utc)))
        finally:store.close()
        return
    store=Store(url)
    try:
        if not store.lock():
            if args.migrate:raise RuntimeError('Database is busy; migrations did not run. Retry deployment after the active job completes.')
            event('scan_already_running');return
        store.migrate()
        boundary=load_boundary(settings.boundary_file)
        from .criteria import reevaluate
        changed=reevaluate(store,boundary,dt.datetime.now(dt.timezone.utc))
        event('stored_criteria_rechecked',changed=changed)
        if args.migrate:event('migrations_complete');return
        if args.realscout_requeue:
            if not rs_settings.ingest_enabled:raise PermissionError('Authorized ingestion must be enabled before recovery')
            from .realscout_worker import requeue
            requeue(store,args.realscout_requeue,dt.datetime.now(dt.timezone.utc))
            event('realscout_job_requeued',count=1);return
        if args.realscout_worker_once:
            from .realscout_worker import run_once
            event('realscout_worker_result',**run_once(store,rs_settings));return
        if settings.discovery_source=='unified' and (args.scheduled or args.scheduled_email):
            if not (discovery_settings.enabled and discovery_settings.daily_enabled):event('unified_daily_disabled');return
            from .discovery.runtime import daily
            event('unified_daily_result',**daily(store,boundary,settings,day,cfg=discovery_settings));return
        if settings.discovery_source=='realscout' and (args.scheduled or args.scheduled_email):
            if not rs_settings.daily_enabled:event('realscout_daily_disabled');return
            from .realscout_runtime import daily
            daily(store,boundary,settings,day,rs_settings=rs_settings);return
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
        if settings.discovery_source in {'realscout','unified'}:raise PermissionError('Use the explicitly enabled source-aware daily pipeline')
        store.save_setting('runtime',settings.public())
        store.save_setting('recipient',{'email_to':settings.email_to,'email_from':settings.email_from})
        store.save_setting('criteria',dict(city='Pittsburgh',state='PA',bedrooms_min=3,bathrooms_min=1,sqft_min=1000,price_max=200000,excluded_zip=['15210','15203','15204'],tax_add=100,tax_strict_max=2000))
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
