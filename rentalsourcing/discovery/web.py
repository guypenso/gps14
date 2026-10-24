"""Existing dashboard Basic auth protects all unified read-only routes."""
import json,os
from flask import jsonify,request,redirect
from .report import snapshot
from ..realscout_store import LinkVault
from ..realscout import safe_url


def register(app,store_factory,settings,clock,boundary):
    @app.get('/api/discovery/report')
    def unified_report():
        if not settings.enabled:return jsonify(error='integration_disabled'),404
        if boundary is None:return jsonify(error='boundary_unavailable'),503
        store=store_factory()
        try:return jsonify(snapshot(store,boundary,clock(),assumptions=settings.assumptions,financing=settings.financing))
        finally:store.close()
    @app.get('/discovery/detail/<entity_id>')
    def unified_detail(entity_id):
        if not settings.enabled:return jsonify(error='integration_disabled'),404
        store=store_factory()
        try:
            for (raw,) in store.execute('SELECT payload FROM discovery_records WHERE entity_id=?',(entity_id,)).fetchall():
                record=json.loads(raw)
                if record['source']['kind']=='realscout':
                    row=store.execute('SELECT detail_ciphertext FROM realscout_candidates WHERE id=?',(record['record_id'],)).fetchone()
                    if row and row[0]:
                        link=safe_url(LinkVault(os.getenv('REALSCOUT_LINK_KEY','')).decrypt(row[0]))
                        if link:return redirect(link,302)
            return jsonify(error='private_link_unavailable'),404
        except Exception:return jsonify(error='private_link_unavailable'),404
        finally:store.close()
