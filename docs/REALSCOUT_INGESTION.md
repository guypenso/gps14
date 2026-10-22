# RealScout production integration

Implemented locally against the maintained `guypenso/gps14` master revision `051e11a`. This extends the existing Flask/Render/PostgreSQL application, not the standalone prototype. No production changes, mailbox access, paid API calls, real email sends, quota increases, remote pushes or forwarding setup were performed. All new flags default off; legacy discovery and delivery remain the defaults. The live deployment/account configuration has not been inspected because no deployment credentials are available.

## Implemented flow

Resend signed `email.received` notification -> authenticated public POST route -> PostgreSQL durable job -> bounded supported Receiving API retrieval -> cryptographically verified original sender/destination -> profile-specific HTML/MIME/plain-text parser -> candidate/observation history -> original hard filters -> cached or explicitly enabled selective RentCast evidence -> ranked, source-aware report -> EXISTING daily outbox/Resend sender.

- POST `/webhooks/resend/inbound`: official Svix verification of raw bytes and timestamp; only this POST is exempt from dashboard Basic auth. Invalid signatures 401, unauthorized sender/destination 403, unavailable durable queue 503, retries/duplicates 200. Disabled endpoint 404. Maximum webhook body 128 KiB.
- Worker GET `https://api.resend.com/emails/receiving/<UUID>`: supported receiving endpoint, separate receiving key, fixed HTTPS host/path, no redirects, 30-second timeout, 2 MB response bound, exact returned ID. No mailbox enumeration, attachment download, detail-link resolution, images or portal crawling. Data-URI HTML is decoded locally.
- PG `SKIP LOCKED` claims, per-job leases, bounded exponential retry, dead-job recovery/quarantine, heartbeats and operator requeue survive restarts. Each existing Gunicorn worker may run a queue thread; PostgreSQL prevents duplicate claims. The daily job also drains at most 20 jobs. No new worker service or cron required.
- Template profiles are separate: `exclusive_alert` is validated on supplied original; `saved_search` is synthetic-tested and blocked from activation until a real sample is validated. Unknown/malformed templates quarantine. The sample's text/plain is MJML and is parsed as markup, not assumed ordinary text.
- Multi-card, unit-aware address keys, exact listing links, optional explicit MLS ID/status, source profile/label, source date, processing date, days-on-site, price history and versioned reviews. Replays idempotent; equal-time conflicts quarantine; delayed observations cannot overwrite newer prices. Site days is not MLS DOM; Exclusive/New Listing is not Active.
- Links are Fernet encrypted separately; private bearer tokens are absent from report/outbox/logs. Reports link to `/realscout/detail/<candidate>` behind existing dashboard Basic auth; only a user click redirects to the original URL. Back up the link key separately from the DB. No key rotation command is supplied: preserve the original key until a reviewed re-encryption migration is available.
- `/api/realscout/status` and `/api/realscout/report` are private Basic-auth GETs. Existing dashboard remains intact. The new report endpoint exposes source-aware candidates; the old dashboard inventory table remains RentCast history.

## Cryptographic sender authentication

The old configurable Authentication-Results trust path is retired and cannot be enabled with an
approval string. The app verifies fully signed original MIME using exact-domain-aligned RSA2048+
DKIM and an explicit signed original-recipient allowlist. Provider-projected HTML/headers and
received_for never establish authenticity. A dedicated alias is routing, not sender authentication.
See [verified research, implementation, configuration and smallest controlled test](REALSCOUT_AUTHENTICATION.md).
Raw-message availability is supported by the official SDK but optional and omitted in the public
OpenAPI response schema; it and actual forwarding preservation still require one approved receipt.
All verification remains disabled. No new service/subscription is required.

## Original investment policy preserved

Calls existing `core.verification` and `core.tax_verification`:

- Within shipped, provenance-checked official Pittsburgh municipal polygon, PA, confirmed Active.
- At least 3 bedrooms, 1 bathroom, 1,000 sqft; price at most $200,000.
- ZIP 15203, 15204, 15210 excluded.
- Latest available reported annual property tax + $100 **strictly below $2,000**. Annual $1,900 rejects; $1,899.99 passes this check.

Postal Pittsburgh is not municipal proof. Missing coordinates, current status, taxes, attributes or identity stay Pending Verification. Provenance/matching/staleness checks cannot be bypassed with a template label. Source email dates remain distinct from processing dates, so forwarding old email today does not make its listing observation current. Selective status evidence must be at least as new as the source observation and match price/beds/baths/area; discrepancies remain pending or reject known failed thresholds.

No recovered rent/HOA/insurance/financing/yield thresholds exist in current code. Do not invent them. Financial qualification here means the original tax rule plus all hard property/location/status filters. Rent, yield and cash flow remain unavailable when unsupported. Ranking is explicitly **tax headroom**, `100*(2000-adjusted annual tax)/2000`, qualified candidates only, then price/sqft. This is not ROI or an invented pass threshold.

## Selective enrichment and spending

`DISCOVERY_SOURCE=realscout` blocks bulk RentCast listing discovery. First match a unique exact normalized cached address/unit. Reuse existing property records, tax_history and response_cache. Explicit optional enrichment uses `/properties?address=<one address>&limit=1` or known property ID. Optional status confirmation uses `/listings/sale?address=<one address>&status=Active&limit=1`; this is a per-candidate verification request, never a city inventory scan.

Tax and status share the original Client, one-time/billing/day/cycle/run request reservations and persistent usage/api_attempts. Up to **two uncached requests per selected candidate** (record + status), not a guarantee of one call. Off by default and requires existing separate RentCast spending approval. Run/day cap stops the loop; newest locally eligible candidates are considered first. Failed/negative response caches suppress repeat attempts; HTTP retries default zero; tax cache default 7 days, negative 6 hours, exact sale-response cache 15 minutes. Current status evidence valid up to 24 hours. No new allowance IDs or counter resets are created. Crashes after a paid response can recover from durable response_cache even when remaining allowance is zero. Tax refresh keeps the original fair-share calculation, without reserving city listing pages in RealScout mode.

No rental AVM request is implemented because neither source data nor an approved rental policy is available. The integration never substitutes assumed rent/tax/expenses. Provider-wide account usage/plan is not inferable from application counters; existing caps are app-side ceilings, not verified provider entitlement.

## Daily delivery and freshness

Keep existing Render cron `0 5,6 * * *`, same `python -m rentalsourcing.cloud --scheduled-email`. Existing Israel 08:00 guard allows 05:00 UTC in summer / 06:00 UTC in winter, not two daily sends. No new cron, systemd unit or GitHub schedule. When explicitly switched to RealScout+DAILY, that same command drains ingestion before report preparation; it does not run legacy bulk discovery.

Existing `daily:<Israeli-date>` outbox key is shared. Accepted/attempted legacy daily report cannot be displaced. A previously unattempted draft may be replaced without losing legacy queued events. Frozen payload, Resend idempotency, 23-hour uncertain-send window, sent reconciliation and definitive-rejection behavior are retained. Repeated accepted daily runs return before retrieval/enrichment. At send time candidate versions/claimed qualification are rechecked under shared PG lock; stale unattempted reports regenerate, attempted ambiguous sends require reconciliation rather than changing the body.

Reports separate new-to-system, price changes, prior reviews, financial/listing verification and qualified/rejected. Source timestamps and missing evidence are visible. Report windows begin at previous accepted report cutoff; on initial activation, current Israeli midnight is the cutoff start. Earlier history remains labeled previously seen, never a fresh scan. A no-alert day says **no new alerts were ingested**, not that no listings exist. Email alerts are a partial event stream, not a complete fresh MLS inventory. Only current queue/worker/budget state is shown, not stale historical warnings.

## Offline validation

Run developer tests with `python -m pytest -q`. Provider network is blocked by autouse fixture. For PostgreSQL set `TEST_DATABASE_URL` to a **disposable local test DB only**; tests create/drop unique schemas. Do not point this at production. Optional `REALSCOUT_SAMPLE_EML` and `REALSCOUT_SAMPLE_EXPECTED_JSON` point to authorized private source/expected files, never committed. Pinned dependencies in requirements.txt/dev include svix and cryptography.

Generated reports through maintained modules:

```
python scripts/realscout_dry_run.py --eml /private/authorized.eml --profile exclusive_alert --output-dir /private/preview
python scripts/realscout_dry_run.py --synthetic --output-dir /private/synthetic-preview
```

These use an in-memory DB and ephemeral encryption key, no provider clients, mailbox access, sends or permanent state. Outputs are text/HTML/JSON; links point to an explicitly non-production example.invalid dashboard, never expose sample tokens. The supplied sample has 3 cards, 1 rejected, 2 pending, 0 qualified. Synthetic output demonstrates a ranked qualification and a price reduction and is visibly labeled fictional. See SAMPLE_VALIDATION for exact source values and test notes; DEPLOYMENT for staged configuration/recovery.

References (public documentation; account eligibility/contract still needs verification):
- https://resend.com/docs/dashboard/receiving/introduction
- https://resend.com/docs/api-reference/emails/retrieve-received-email
- https://resend.com/docs/webhooks/verify-webhooks-requests
- https://support.google.com/mail/answer/10957
- https://support.google.com/mail/answer/6579
