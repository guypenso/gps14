"""Existing daily job: MLS first, isolated sources, selective evidence, same outbox."""
import datetime as dt
import json,os,hashlib
from .config import DiscoverySettings
from .connectors import ResoConnector,CSVConnector,WPRDCConnector
from .adapters import RentCastAdapter,sync_realscout,sync_legacy_rentcast
from .repository import register,ingest,health,resolution
from .report import prepare
from ..collector import Client
from ..digest import Digest


def build(profile,store,settings,now,cfg):
    source=profile['source'];transport=profile['transport']
    if transport in {'trestle','reso'}:
        base=profile.get('base_url') or 'https://api.cotality.com/trestle/odata/'
        return ResoConnector(source,base,token=os.getenv(profile.get('bearer_env','MLS_BEARER_TOKEN')),
            token_url=profile.get('token_url') or ('https://api.cotality.com/trestle/oidc/connect/token' if transport=='trestle' else None),
            client_id=os.getenv('TRESTLE_CLIENT_ID'),client_secret=os.getenv('TRESTLE_CLIENT_SECRET'),
            expected_system=profile.get('expected_system'),max_pages=profile.get('max_pages',20))
    if transport=='rentcast':return RentCastAdapter(source,Client(store,settings),discovery_enabled=cfg.rentcast_discovery,rent_enabled=settings.rent_estimates_enabled,status_enabled=cfg.rentcast_status)
    if transport=='wprdc':return WPRDCConnector(source)
    return None


def daily(store,boundary,settings,day,*,cfg=None,clock=None,providers=None,opener=None):
    cfg=cfg or DiscoverySettings.from_env();clock=clock or (lambda:dt.datetime.now(dt.timezone.utc))
    if not(cfg.enabled and cfg.daily_enabled and settings.discovery_source=='unified'):raise PermissionError('Unified daily pipeline is disabled')
    existing=store.execute('SELECT id,state,first_attempt_at FROM outbox WHERE job_id=?',('daily:'+day,)).fetchone()
    if existing and existing[1]=='sent':return dict(status='already_sent',outbox_id=existing[0],sources=[])
    if existing and existing[2]:
        if existing[1]=='failed':return dict(status='rejected_requires_review',outbox_id=existing[0],sources=[])
        status=Digest(store,boundary,settings,clock,opener,discovery_settings=cfg).send(existing[0]) if settings.email_enabled else 'prepared_not_sent'
        return dict(status=status,outbox_id=existing[0],sources=[])
    results=[];rental=None
    profiles=sorted(cfg.sources,key=lambda p:p['source'].kind!='mls')
    for profile in profiles:
        source=profile['source'];register(store,source)
        if not source.enabled:continue
        try:
            provider=(providers or {}).get(source.name) or build(profile,store,settings,clock(),cfg)
            if source.kind=='realscout':
                results.append(sync_realscout(store,clock(),source,public_app_url=os.getenv('PUBLIC_APP_URL','')));continue
            if isinstance(provider,RentCastAdapter):
                rental=provider;sync_legacy_rentcast(store,source,clock())
                last=store.execute('SELECT last_success FROM discovery_sources WHERE name=?',(source.name,)).fetchone()[0]
                due=not last or clock()-dt.datetime.fromisoformat(last)>=dt.timedelta(days=cfg.rentcast_interval_days)
                if due:results.append(provider.scan(store,clock()))
                else:results.append(dict(source=source.name,mode='discovery_not_due',fresh_scan=False))
            elif isinstance(provider,WPRDCConnector):
                count=0
                for (eid,) in store.execute('SELECT id FROM discovery_entities').fetchall():
                    if count>=cfg.max_candidates:break
                    fields=resolution(store,eid,clock())['fields']
                    if fields.get('parcel_id') and fields.get('county')=='Allegheny':
                        item=provider.lookup(fields['parcel_id'],clock());ingest(store,item,clock(),authorized=True);count+=1
                results.append(dict(source=source.name,mode='public_parcel_lookup',records=count,fresh_scan=False))
                health(store,source.name,clock(),success=count>0)
            elif profile['transport']=='csv_webhook':results.append(dict(source=source.name,mode='awaiting_authorized_export',fresh_scan=False))
            elif profile['transport']=='csv_file':
                from pathlib import Path
                # File is an approved staged export; producer supplies source snapshot timestamp per row.
                path=Path(profile['export_path']);raw=path.read_bytes();manifest=json.loads(Path(profile['manifest_path']).read_text())
                if manifest.get('source')!=source.name or manifest.get('sha256')!=hashlib.sha256(raw).hexdigest():raise ValueError('Export manifest/body mismatch')
                stamp=manifest['exported_at']
                results.append(CSVConnector(source).import_bytes(store,raw,stamp,clock(),authorized=True))
            elif provider is not None:
                result=provider.scan(store,clock());results.append(result)
                if result.get('complete') and cfg.reverify_limit and hasattr(provider,'verify_keys'):
                    cutoff=(clock()-dt.timedelta(hours=24)).isoformat()
                    keys=[r[0] for r in store.execute('SELECT record_id FROM discovery_records WHERE source=? AND observed_at<? ORDER BY observed_at LIMIT ?',(source.name,cutoff,cfg.reverify_limit)).fetchall()]
                    if keys:results.append(dict(source=source.name,point_verification=provider.verify_keys(store,keys,clock())))
        except Exception as exc:
            health(store,source.name,clock(),error=type(exc).__name__);results.append(dict(source=source.name,error=type(exc).__name__,fresh_scan=False))
    if rental and cfg.rentcast_enrichment:
        try:results.append(dict(source=rental.source.name,enrichment=rental.enrich(store,boundary,clock(),cfg.max_candidates)))
        except Exception as exc:results.append(dict(source=rental.source.name,enrichment_error=type(exc).__name__))
    identity=prepare(store,boundary,settings,cfg,day,clock(),source_results=results)
    status=Digest(store,boundary,settings,clock,opener,discovery_settings=cfg).send(identity) if settings.email_enabled else 'prepared_not_sent'
    return dict(status=status,outbox_id=identity,sources=results)
