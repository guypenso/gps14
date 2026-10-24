"""Broker-authorized CSV delivery to EXISTING web service; raw-body HMAC + durable replay guard."""
import datetime as dt
import hashlib,hmac,os,re
from flask import request,jsonify
from .connectors import CSVConnector

PREFIX='/webhooks/mls/export/'


def verify(raw,headers,secret,now,*,source_name=''):
    if len(secret)<32:raise PermissionError('Export signing secret unavailable')
    stamp=headers.get('X-MLS-Timestamp','');nonce=headers.get('X-MLS-Event','');exported=headers.get('X-MLS-Exported-At','')
    if not re.fullmatch(r'\d{10}',stamp) or abs(now.timestamp()-int(stamp))>300:raise PermissionError('Expired export signature')
    if not re.fullmatch(r'[A-Za-z0-9_-]{16,100}',nonce):raise PermissionError('Invalid export event')
    from .models import instant
    instant(exported)
    material=(source_name+'.'+stamp+'.'+nonce+'.'+exported+'.').encode()+raw
    expected=hmac.new(secret.encode(),material,hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected,headers.get('X-MLS-Signature','')):raise PermissionError('Invalid export signature')
    return nonce,exported,hashlib.sha256(raw).hexdigest()


def register(app,store_factory,settings,clock):
    @app.post(PREFIX+'<source_name>')
    def receive(source_name):
        if not (settings.enabled and settings.export_webhook_enabled):return jsonify(error='integration_disabled'),404
        profile=next((p for p in settings.sources if p['source'].name==source_name and p['source'].enabled and p.get('transport')=='csv_webhook'),None)
        if profile is None:return jsonify(error='source_not_authorized'),403
        request.max_content_length=10_000_000;raw=request.get_data(cache=False)
        try:
            event,exported,digest=verify(raw,request.headers,os.getenv(profile.get('secret_env',''),''),clock(),source_name=source_name)
        except (PermissionError,ValueError):return jsonify(error='invalid_export_authentication'),401
        store=store_factory()
        try:
            # Shared advisory lock serializes event replays with all other ingestion/report writes.
            if store.pg:store.execute('SELECT pg_advisory_lock(73541209)')
            old=store.execute('SELECT body_hash FROM discovery_export_events WHERE source=? AND event=?',(source_name,event)).fetchone()
            if old:return (jsonify(status='duplicate'),200) if old[0]==digest else (jsonify(error='identity_collision'),409)
            result=CSVConnector(profile['source']).import_bytes(store,raw,exported,clock(),authorized=True)
            store.execute('INSERT INTO discovery_export_events VALUES (?,?,?,?)',(source_name,event,digest,clock().isoformat()))
            return jsonify(status='accepted',records=result['records']),200
        except (ValueError,UnicodeError):
            from .repository import health
            health(store,source_name,clock(),error='export_requires_review')
            return jsonify(error='export_requires_review'),422
        except Exception:return jsonify(error='temporary_ingestion_failure'),503
        finally:store.close()
