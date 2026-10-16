# Validation record — 2026-10-16

- Python 3.12 isolated virtual environment; production dependencies installed successfully.
- PostgreSQL 16.15 in a local disposable Docker container; no managed/paid database created.
- `python -m pytest -q` with TEST_DATABASE_URL: **98 passed, 1 skipped**.
- The one skip is the SQLite parameter of the PostgreSQL advisory-lock test; the PostgreSQL parameter passed.
- Runtime compilation, Render/Railway configuration syntax and disabled-provider defaults checked.
- Repository content checked against configured secret values before publication; no secret values committed.
- Both provider HTTP transports are mocked in tests. The normal urllib opener is blocked by an autouse fixture.
- No live RentCast API requests, real Resend messages, paid deployment or purchases were made.
- Real external acceptance is still required: official geometry download in Render, RentCast response,
  real tax enrichment and Resend acceptance/inbox check, using explicitly approved allowances.

Covered behaviors include persistent PostgreSQL state, concurrent-run exclusion, pagination/resume,
new and missing-field properties, strict tax thresholds, price-reduction events, caches/expiry,
request approval caps across billing boundaries, DNS/proxy/TLS/timeout/rate-limit handling,
daily-digest de-duplication, same-key retries and manual reconciliation after the safety window,
permanent mail rejection, stale snapshot cancellation, output escaping and 2026–2028 DST schedules.

## Controlled 100-listing POC update

The POC extension was validated with **105 passed, 1 skipped** against SQLite and local PostgreSQL.
The added tests verify exact URL decoding, plan-only execution, one listing page, a maximum of three
unique tax candidates across resumes, complete initial counts, source/year/amount report fields,
unknown coverage and absent listing links. No real RentCast/Resend requests were made.

## Production dashboard and launch acceptance

- Full suite with local PostgreSQL: **137 passed, 1 skipped** (SQLite variant of the PostgreSQL-only lock test).
- Gunicorn with two workers was exercised over real loopback HTTP against the persisted PostgreSQL volume.
  Dashboard, JSON API, email-preview and health routes returned 200. Readiness correctly returned 503
  when official city geometry was unavailable. The database contained one genuine failed acceptance scan,
  one counted RentCast attempt and zero received properties; no test properties were put in this database.
- Authentication, escaped output, security headers, no-provider-on-refresh, stale-data downgrades,
  recurring allowance across billing boundaries and a 95-day daily allocation were verified locally.
- Actual RentCast acceptance: one authorized attempt returned HTTPError / Forbidden, with no listing body.
  No automatic retry and no tax requests were made. No claim of successful live collection is made.
- Actual public GIS download: denied at proxy CONNECT (403), without a fake boundary fallback.
- Resend is tested with controlled responses only; no real message was sent and no Resend key was available.
- No Render resource was created. The new Web + Cron + PostgreSQL Blueprint requires payment approval.
