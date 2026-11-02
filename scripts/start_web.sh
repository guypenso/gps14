#!/usr/bin/env sh
set -eu
python -m rentalsourcing.cloud --migrate
# Explicit, separately gated stored-data job; never a scan, email or schema migration.
if [ "${ENABLE_REVIEW_INVENTORY_STARTUP:-false}" = "true" ]; then
  python -m rentalsourcing.review_inventory --unattended --execute --download-public-data || echo 'Stored review inventory deferred; existing web service will still start.'
fi
if [ "${ENABLE_REVIEW_INVENTORY_STARTUP:-false}" = "true" ]; then
  python -m rentalsourcing.listing_enrichment || echo 'Optional listing enrichment deferred; web service will still start.'
fi
exec gunicorn --bind "0.0.0.0:${PORT:-10000}" --workers 2 --timeout 60 --access-logfile - --error-logfile - 'rentalsourcing.dashboard:create_app()'
