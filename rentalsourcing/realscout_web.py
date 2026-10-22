"""Signature-authenticated webhook and dashboard-authenticated private inspection."""
import datetime as dt
import json
from email.utils import parseaddr
from flask import request,jsonify,redirect
from svix.webhooks import Webhook,WebhookVerificationError
from .realscout_inbound import accept_notification
from .realscout_store import LinkVault
from .realscout_worker import monitor,health
from .telemetry import event

WEBHOOK_PATH='/webhooks/resend/inbound'


def register(app,store_factory,settings,clock,boundary=None):
    @app.post(WEBHOOK_PATH)
    def inbound():
        if not settings.ingest_enabled:return jsonify(error='integration_disabled'),404
        raw=request.get_data(cache=False)
        if len(raw)>131072:return jsonify(error='payload_too_large'),413
        try:
            Webhook(settings.webhook_secret).verify(raw,dict(request.headers))
        except (WebhookVerificationError,ValueError):return jsonify(error='invalid_signature'),401
        try:
            data=json.loads(raw)
            if data.get('type')!='email.received':return jsonify(status='ignored'),200
            recipients={parseaddr(v)[1].lower() for v in data['data']['to']}
            policies=[p for p in settings.policies if p.inbox.lower() in recipients]
            if len(policies)!=1:return jsonify(error='destination_not_authorized'),403
            policy=policies[0]
            sender=parseaddr(data['data'].get('from',''))[1].lower()
            if sender and sender not in {s.lower() for s in policy.allowed_senders}:
                return jsonify(error='sender_not_authorized'),403
        except (ValueError,KeyError,TypeError):return jsonify(error='invalid_event'),400
        store=None
        try:
            store=store_factory()
            result=accept_notification(store,raw,dict(request.headers),signing_secret=settings.webhook_secret,policy=policy)
            health(store,'webhook',{'last_event':'accepted'},clock())
            return jsonify(status='duplicate' if result['duplicate'] else 'queued'),200
        except (ValueError,KeyError,TypeError):return jsonify(error='invalid_event'),400
        except Exception:
            event('realscout_webhook_unavailable',category='durable_queue_unavailable')
            return jsonify(error='temporary_queue_failure'),503  # Never ACK an uncommitted job.
        finally:
            if store:store.close()

    @app.get('/api/realscout/status')
    def status():
        store=store_factory()
        try:return jsonify(monitor(store,clock()))
        finally:store.close()

    @app.get('/api/realscout/report')
    def current_report():
        if boundary is None:return jsonify(error='boundary_unavailable'),503
        from .realscout_report import report
        store=store_factory()
        try:return jsonify(report(store,boundary,clock(),public_url=settings.public_url))
        finally:store.close()

    @app.get('/realscout/detail/<candidate_id>')
    def detail(candidate_id):
        if not (settings.daily_enabled or settings.ingest_enabled):return jsonify(error='integration_disabled'),404
        store=store_factory()
        try:
            row=store.execute('SELECT detail_ciphertext FROM realscout_candidates WHERE id=?',(candidate_id,)).fetchone()
            if not row or not row[0]:return jsonify(error='link_unavailable'),404
            from .realscout import safe_url
            url=safe_url(LinkVault(settings.link_key).decrypt(row[0]))
            if not url:return jsonify(error='link_unavailable'),404
            return redirect(url,302)  # User click only; server never visits the destination.
        except Exception:return jsonify(error='link_unavailable'),404
        finally:store.close()
