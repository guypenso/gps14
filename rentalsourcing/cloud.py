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
from .schedule import scheduled_day,PITTSBURGH
from .storage import Store
from .telemetry import event


def main(argv=None):
    parser=argparse.ArgumentParser(description='RentalSourcing daily production collector')
    group=parser.add_mutually_exclusive_group()
    group.add_argument('--check',action='store_true',help='Check config and boundary offline; no provider calls')
    group.add_argument('--scheduled',action='store_true',help='Run at 08:00 Pittsburgh; resume a failed day safely')
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
        exists=Path(settings.boundary_file).is_file()
        if exists:load_boundary(settings.boundary_file)
        event('preflight',boundary_valid=exists,api_approved=settings.approved_requests>0 and bool(settings.approval_id),
              configured=settings.public(),database_configured=bool(os.getenv('DATABASE_URL')),
              rentcast_secret_configured=bool(os.getenv('RENTCAST_API_KEY')),resend_secret_configured=bool(os.getenv('RESEND_API_KEY')))
        if not exists:raise ValueError('Official city boundary missing; run scripts/fetch_boundary.py in the deployment environment')
        return
    day=dt.datetime.now(dt.timezone.utc).astimezone(PITTSBURGH).date().isoformat()
    if args.scheduled:
        day=scheduled_day()
        if day is None:event('outside_pittsburgh_schedule');return
    administrative=args.status or args.migrate or args.retry_email or args.reconcile_email
    if not administrative and not settings.enabled:event('collector_disabled');return
    url=os.getenv('DATABASE_URL','')
    if not url.startswith(('postgres://','postgresql://')):raise ValueError('Production requires persistent PostgreSQL DATABASE_URL')
    store=Store(url)
    try:
        if not store.lock():event('scan_already_running');return
        store.migrate()
        if args.migrate:event('migrations_complete');return
        if args.status:
            event('status',budget=store.budget_health(settings),
              jobs=store.execute('SELECT id,state,offset_value,collection_done FROM scan_jobs ORDER BY updated_at DESC LIMIT 10').fetchall(),
              states=store.execute('SELECT state,COUNT(*) FROM properties GROUP BY state').fetchall(),
              outbox=store.execute('SELECT id,state,provider_id FROM outbox ORDER BY updated_at DESC LIMIT 10').fetchall(),
              operations=store.operations());return
        boundary=load_boundary(settings.boundary_file)
        digest=Digest(store,boundary,settings)
        if args.retry_email:digest.send(args.retry_email);return
        if args.reconcile_email:digest.reconcile(args.reconcile_email,args.provider_message_id);return
        store.save_setting('runtime',settings.public())
        store.save_setting('recipient',{'email_to':settings.email_to,'email_from':settings.email_from})
        store.save_setting('criteria',dict(city='Pittsburgh',state='PA',bedrooms_min=3,bathrooms_min=1,sqft_min=1000,price_max=200000,excluded_zip=['15210','15203'],tax_add=100,tax_strict_max=2000))
        job_id=args.resume or ('daily:'+day if args.scheduled else 'manual:'+day+':'+str(uuid.uuid4()))
        if args.resume:
            job=store.execute('SELECT day FROM scan_jobs WHERE id=?',(job_id,)).fetchone()
            if not job:raise ValueError('Unknown scan to resume')
            day=job[0]
        health=store.budget_health(settings)
        if args.scheduled:
            # Keep allowance available for later days instead of exhausting it early.
            fair_share=max(1,health['remaining']//health['days_remaining']) if health['remaining'] else 0
            settings=replace(settings,per_run=max(1,min(settings.per_run,fair_share or 1)))
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
