# Final launch validation — 2026-10-16

188 passed, 3 skipped against Python 3.12 + actual local PostgreSQL 17.11 and SQLite.
The three skips are SQLite variants of PostgreSQL-only lock/concurrency checks;
the PostgreSQL variants passed. Provider requests are blocked/mocked throughout tests.
No additional live RentCast request or real email was sent in this launch cycle.

Verified: additive migrations, durable records and quotas, concurrent reservations
and claims, duplicate notifications, cursors/pages beyond 100, carried pending
work across dates, negative-tax cache refresh, price-cut priority, strict tax
threshold/latest-year handling, three years of DST, digest error reporting,
listing-link safety, stale/future timestamp refusal, private dashboard, operational
errors, state transfer atomicity and preservation of spent approval/outbox history.

Actual production start command `sh scripts/start_web.sh` launched two Gunicorn
workers against the existing live-data PostgreSQL. HTTP: /healthz 200; no-auth / 401;
authenticated /readyz, HTML, /api/dashboard, /api/email-preview 200. Dashboard read
100 real listings with three fully qualified properties. The usage ledger stayed
at four attempts and outbox stayed empty. No demo fallback or provider calls.

Actual PostgreSQL-to-PostgreSQL transfer rehearsal copied 100 listings, three tax
records, approval/cycle/day usage, history/events/jobs to an isolated empty schema.
The original source remained unchanged. Tests also prove nonempty targets are
refused and schema incompatibility rolls back all copied rows.

Render YAML/deployment invariants and duplicate-YAML-key checks passed locally.
Both services deploy master only after CI passes, verify bundled official geometry
offline and keep providers disabled/allowance zero. Public HTTPS, Render database,
scheduler execution and Resend acceptance remain unverified until authorized
provisioning, secrets and explicit mail/API approval. Local smoke is not production.
