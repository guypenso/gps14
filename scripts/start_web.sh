#!/usr/bin/env sh
set -eu
python -m rentalsourcing.cloud --migrate
# The supervised in-process watchdog audits first, then resumes stored syncs.
# Startup never scans providers, sends mail, or runs an unaudited backfill.
exec gunicorn --bind "0.0.0.0:${PORT:-10000}" --workers 2 --timeout 60 --access-logfile - --error-logfile - 'rentalsourcing.dashboard:create_app()'
