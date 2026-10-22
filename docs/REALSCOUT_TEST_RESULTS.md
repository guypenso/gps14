# Offline validation — October 22, 2026

Base: maintained guypenso/gps14 master `051e11a11835b5e275dea38e8ebeb71c39602ff9`.
Changes are local only. No deployment, remote push, live property requests, mailbox access or real sending.

## Final result

**412 passed, 7 skipped in 29.82 seconds** (`python -m pytest -q -rs`).
Pinned production/development dependencies installed in isolated investigation virtualenv.
`pip check`: no broken requirements. `compileall` and `git diff --check`: passed.

SQLite and disposable PostgreSQL 16 (matching CI), plus the earlier PostgreSQL 17 run, were exercised. PG fixture creates/drops a unique schema per
case, including concurrency tests. Seven skips are the SQLite parametrizations of tests explicitly
requiring PG cross-session concurrency, advisory locking or read-only enforcement; their PostgreSQL
variants ran. The three original-sample tests ran with explicit REALSCOUT_SAMPLE_EML pointing to the
user-supplied .eml. No PostgreSQL test was skipped because of missing test-server access.

Every provider transport is mocked/blocked by the autouse network fixture. Real cloud API keys are
replaced by fake test values. Test-only receiving clients return synthetic messages; fake Resend
acceptance is not claimed as live delivery. Test-only RentCast responses prove code behavior, not
actual account plan/endpoint availability or actual property tax values.

## Covered behavior

- Supplied three-card Exclusives HTML and MJML alternatives, exact raw href equality, state/ZIP/
  price/bed/bath/area/site-days extraction; no opening links/images. Separate regular template fixtures.
- Plain/HTML multi-card messages, missing/ambiguous fields, malformed/oversized input, bounded tree,
  tracking-link redaction, optional explicit listing ID/status and labeled previous/current price.
- Normalized addresses/units, repeated message/card detection, identity collisions, duplicate events,
  out-of-order alerts, price increases/reductions, first alert that is also a labeled price change,
  review-version invalidation, source-time staleness vs import time.
- Official criteria, boundary/ZIP checks, missing evidence pending, tax latest year/strict $1,900
  rejection, source/identity/demo mismatch, current status discrepancies and expired evidence.
- Svix signature/timestamp/tamper, wrong recipient/sender, invalid/oversized events, persistence
  failure503, Basic-auth report/status/link protection, private tokens absent from report/outbox.
- Production transport discards undocumented top-level authentication claims; explicit trusted
  receiver-header contract, exact authserv-id and DMARC From-domain alignment, malformed/duplicate/
  spoofed header rejection. Missing actual provider contract still blocks live activation.
- Receiving fixed HTTPS UUID endpoint, ID binding/no redirects; 429/retry/quarantine, crashed leases,
  ingest-before-ACK replay, multiple PG workers each claiming a message once.
- Exact-address enrichment, no city inventory query in RealScout mode, existing approvals/run/day/
  billing counters and caches, negative caching, zero-allowance recovery from paid response cache,
  optional one-address status lookup sharing the cap, no quota reset.
- Shared legacy daily outbox key, old report conflicts, same-day no repeat retrieval/enrichment/send,
  stale qualification/version cancellation, uncertain send frozen body/key and 23-hour guard,
  failed delivery, API budget failures, full fake receiving->parse->verify->rank->fake-send.
- Existing legacy test suite remains green. Existing history/cache/quotas/outbox stay intact.

## Actual startup smoke test

Executed existing `sh scripts/start_web.sh` with all provider/new feature flags disabled, fake keys,
local Gunicorn and a unique schema in the disposable PostgreSQL instance. Additive migrations
completed; both Gunicorn workers started; `/healthz`200; private `/api/realscout/status`401 and
`/api/realscout/report`401; disabled POST webhook404. Test schema dropped and test process stopped.
The test container was removed after completion. No production service/database was contacted.

Manifest parsed: exactly the original daily cron, expression `0 5,6 * * *`, command
`python -m rentalsourcing.cloud --scheduled-email`. Offline cloud preflight passed, boundary valid,
provider/send flags disabled, no configured live DB. This does not verify live Render/VPS schedules.

## Reports and privacy

Original-sample report and exact expected values are kept privately outside Git. The published
branch contains only a redacted validation record; no source addresses, sender/contact headers,
private observation timestamps or real-email report artifacts. The optional original tests also
require REALSCOUT_SAMPLE_EXPECTED_JSON, a private JSON array of expected field tuples.

`docs/examples/realscout-synthetic/report.{txt,html,json}`: explicitly synthetic evidence, 3 candidates,
1 price change, 1 qualified (tax-headroom45), 1 pending, 1 rejected. Demonstrates renderer/scoring;
not a recommendation about a real property. Example dashboard links are inert placeholders.

Scanned deliverables against all three original private detail URLs: zero matches. Original raw
sample/contact headers and bearer tokens are not included in fixtures or report artifacts.

## Limits of proof

Not verified: live Resend receiving account/cost/permissions/header trust, actual Gmail forwarding,
actual regular saved-search template, live sender/recipient delivery issue, production deployment/
backups/cron inventory, provider-wide usage or property data. No offline test establishes ROI or
complete MLS coverage. See REALSCOUT_DEPLOYMENT.md for explicit activation blockers.
