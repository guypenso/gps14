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
