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
- Production transport discards projected authentication claims and never trusts archived verdicts.
  Raw RSA2048/SHA256 verification, exact signing-domain/sender/original-recipient binding, signed
  parsing-critical headers, whole-body integrity and age/expiry checks are exercised cryptographically.
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

## Authentication implementation update — October23, Israel

Final revised suite: **475 passed, 7 skipped in38.84s**, disposable PostgreSQL16 plus SQLite and the
explicitly supplied private original parser expectations. This adds63 cases to the prior419 total;
new crypto tests use genuine generated RSA signatures, mocked DNS wire-format responses and mocked
raw downloads. No provider/DNS/mailbox was accessed. Queue/report regression tests use an explicitly
named test verifier double; cryptographic tests run the real RawDkimVerifier with generated keys.

Covered: spoofed sender, wrong signing domain/key, RSA1024 rejection, body/header tampering,
duplicate critical headers, unsigned critical fields, partial-body signatures, forged Authentication-
Results/JSON verdicts, authorized signed original To, queue alias binding despite forged received_for,
replay under new provider IDs without observation refresh, Svix event dedup, exact HTTPS raw-host
allowlist, no redirected/credential-bearing raw downloads, missing/oversized raw, DNS outages and
CNAME/TTL cache, malformed DNS wire responses, lease loss during verification, audit metadata and
content/token-safe failure logging. Original financial criteria/outbox/budget tests remain green.

Migration2 adds only auth evidence/public DKIM cache tables; live migration/deployment not performed.
The SDK raw capability/account compatibility and actual Gmail preservation still require one approved
receipt. See REALSCOUT_AUTHENTICATION.md for official-source research and exact controlled-test scope.

Final recovery regressions also verify delayed raw availability retries before one successful ingest,
and committed cryptographic evidence can ACK after a restart without repeating private retrieval.
A fresh actual Gunicorn/start_web.sh smoke test on isolated PostgreSQL16 confirmed migration2,
health200 and disabled webhook404. No background worker/provider request or production change.
Offline secret scan:11 candidates, all explicit test placeholders/example attack URLs/public checksum;
no actual credentials, private original email contents, raw URLs or original property expectations.
