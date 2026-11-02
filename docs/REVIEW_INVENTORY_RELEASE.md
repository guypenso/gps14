# Full stored review queue — November 2, 2026

No provider scans, additional paid usage, email resends, SQL migrations or changes
to hard criteria. Keep all records, reviews, tombstones and existing outbox keys.
The morning's natural scan can change yesterday's 123-candidate count: report the
actual current stored selection instead of manufacturing an exact historical set.

## Receiver first

PENTEN adds filters and distinct Verified/Estimated/Unknown tax states; estimates
never satisfy the mandatory bill gate. Freshness is recomputed at read time.
The scoped /api/rentalsourcing/review-sync endpoint accepts pending candidates
only and requires a new private HMAC secret plus existing Sites service access.
/api/rentalsourcing/review-classifications returns signed, nonce-bound read-only
human decisions. Original daily sync credentials/contract remain usable.
No schema changes: metadata is stored in the existing JSON payload. Existing
classifications and rejection tombstones remain authoritative on every upsert.

## Existing Render services

Private new runtime settings (never commit their values):
- ENABLE_REVIEW_INVENTORY_SYNC=true
- COMMAND_CENTER_REVIEW_URL=<existing Site>/api/rentalsourcing/review-sync
- COMMAND_CENTER_REVIEW_SECRET=<matching scoped receiver secret>
- COMMAND_CENTER_REVIEW_SERVICE_TOKEN=<supported Sites service-access token>
- ENABLE_REVIEW_INVENTORY_STARTUP=true on dashboard only
- ENABLE_COMMAND_CENTER_REVIEW_FEEDBACK=true on cron only

start_web.sh explicitly runs the gated one-time stored-review job after existing
schema initialization and before Gunicorn, outside migrations. The job uses no
RentCast or Resend APIs. It pulls current human rejections, validates all non-tax
rules, includes stale candidates with refresh warnings, downloads the public
assessment CSV once, and freezes <=125-card batches in existing settings rows.
Each acknowledgement is durable. Restarts retry only unacknowledged frozen parts;
a completed job is not resent. Failures do not prevent the web service starting.

Existing daily jobs pull classifications before paid enrichment. A feedback
outage conservatively defers paid tax requests while preserving listing discovery
and email delivery. After accepted email handling, the stored review queue syncs
separately; failures cannot change sent-email state. Future rejections therefore
return automatically to RentalSourcing without granting service callers browser
Administrator identity or changing human classifications from automated data.

## Public assessment estimates

Published WPRDC CSV URL/resource and CC0 dataset are documented in public_tax.py.
One streaming download is checksummed and parsed; only matched candidate records
are cached in PostgreSQL. Addresses include unit, ZIP and verified municipality.
Ambiguous parcels stay Unknown. Missing/unsupported exemption flags, recorded
relief, unconfirmed special levies and missing values stay Unknown. Current
Pittsburgh estimates include county, city, school, library and parks levies.
Bellevue remains Unknown until special-levy completeness is established.
School 2026–27 annualized rate and county/municipal 2026 rates are disclosed;
this is not a certified calendar-year bill. No reassessment or post-purchase
assumption is invented. Formula, source, year, inputs and warnings are retained.
Every estimated/unknown opportunity remains Pending Verification, including when
estimated tax plus $100 is below $2,000. Verified bill rules remain unchanged.

## Verification and rollback

Capture affected D1 rental tables before publication and verify their restoration
in an isolated compatible database; unrelated tables/R2 are untouched. After the
startup job, reconcile source counts, batch receipts, source aliases, visible
records, review classifications, rejection tombstones and stored estimate states.
Do not claim completion solely from HTTP acceptance or synthetic tests.

Disable only the new review flags/receiver scoped secret if rollback is needed.
Retain original daily sender/receiver, emails, budgets and schedules. Restore
prior application versions while retaining additive JSON data; never delete
records, reset counters or rewrite sent emails. Code rollback is not DB restore.
