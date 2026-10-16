# Validation evidence — 2026-10-16

## Automated checks

Final local run: **117 passed, 3 skipped**, Python 3.12, PostgreSQL 17.11.
One SQLite test skips the PostgreSQL advisory lock; two SQLite variants skip
concurrent PostgreSQL quota/claim tests. Their PostgreSQL variants passed.
All data/email-provider calls in this suite are mocked. PostgreSQL is real and
isolated from the live sample; no paid cloud database is used for tests.

Checks cover: atomic quotas under eight concurrent connections; full rollback
on exhausted allowance; durable notification/day claims; advisory locking;
additive migrations; persistence across connections; listing/tax cache expiry;
resumption after failed collection; strict tax threshold; missing latest amounts;
price drops; digest deduplication/idempotency; stale evidence; three years of DST
schedule checks; dashboard authentication, read-only queries and redaction.
Bundled geometry checks verify downtown/Oakland inclusion, neighboring
municipalities/border exclusion, and fail-closed checksum tampering.

`python -m rentalsourcing.cloud --check` passed offline with the bundled official
geometry. Render YAML parsing and `git diff --check` passed. CI runs the same
pytest suite with PostgreSQL 16; this local record does not assert remote CI passed.

## Live acceptance (not mocked)

- ArcGIS official city layer: HTTPS succeeded. Eight city features downloaded,
  union validated, WGS84 confirmed and SHA256 recorded in the bundled manifest.
- Exactly FOUR RentCast attempts: one 100-listing page, three tax lookups.
  Persisted approval/cycle/day counters each equal four. No retries or extra pages.
- API reported 422 matching listings. Of 100 sampled: 58 Rejected, 39 Pending
  Verification, 3 Qualified. Thirty-three were outside city limits and 25 in
  excluded ZIPs. Only three candidates received tax verification.
- Tax years returned were 2024/2025; annual totals were $760/$1,611/$1,267.
  All three passed latest available annual tax + $100 < $2,000. This does not
  assert current-year tax, complete city coverage or county-document verification.
- Actual local PostgreSQL stored listings, tax histories, events, cursor and usage.
  A SQL_ASCII local DB exposed text/bytes decoding; explicit UTF8 connections
  fixed it. The same job resumed from persisted listings without another API call.
- Actual HTTP server against the live-sample PostgreSQL: health 200; status without
  authentication 401; authenticated status 200 with 100 properties and 3 Qualified.
- A daily-digest preview was rendered from these three live property/tax records.
  No Resend request or real email was sent. No Render deployment occurred.

## Reproduction

```sh
pip install -r requirements-dev.txt
TEST_DATABASE_URL=postgresql://... python -m pytest -q
python -m rentalsourcing.cloud --check
```

Do not repeat live acceptance: the approved four attempts are exhausted. Never
change approval IDs or reset a database to regain allowance. New API budget,
paid Render provisioning, and real email delivery require explicit approval.
