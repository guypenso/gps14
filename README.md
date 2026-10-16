# RentalSourcing

Pittsburgh investment-property sourcing with RentCast, official city-boundary
validation, annual tax-history filtering, PostgreSQL, an authenticated dashboard,
and a durable daily email digest.

## Verified status — 2026-10-16

Live acceptance used exactly four authorized RentCast attempts: 100 sale listings
and three property-record lookups. The API reported 422 matching listings, of
which only the first 100 were sampled. Fifty-eight were rejected (33 outside city
limits, 25 excluded ZIPs), 42 passed initial verification, three passed the full
tax check, and 39 remain Pending Verification. This is not a citywide scan.

The official eight-feature city boundary and provenance checksum are bundled and
verified offline; scans do not require ArcGIS network access. A real local
PostgreSQL database persists the live sample, tax history and exhausted 4/4
approval ledger. The dashboard was tested over real HTTP against this database.

**Not deployed to Render. No real email sent. No paid resource created.**
Render configuration is ready for review, with provider switches disabled and
new API allowance zero. All four approved API attempts have been consumed;
ongoing daily scans require a new explicit budget and cannot reuse this approval.

See [readiness](READINESS_HE.md), [deployment](docs/RENDER_DEPLOYMENT.md), and
[validation evidence](docs/VALIDATION.md).

## Criteria

Active PA listings inside the official Pittsburgh polygon; ZIP 15210 and 15203
excluded; at least three bedrooms, one bathroom, 1,000 sqft; price at most
$200,000. Missing evidence stays Pending Verification. Unverified locations
cannot qualify. Latest reported non-future annual tax plus $100 must be strictly
less than $2,000. A missing latest-year amount cannot fall back to an older year.
Tax year is always displayed; latest available does not promise current-year tax.

## Run checks

```sh
pip install -r requirements-dev.txt
python -m pytest -q
python -m rentalsourcing.cloud --check
```

Set `TEST_DATABASE_URL` to an isolated local PostgreSQL test database to include
integration checks. Never point tests at production. Tests mock provider calls.

## Dashboard

```sh
python -m rentalsourcing.dashboard
```

Requires PostgreSQL `DATABASE_URL` and `DASHBOARD_TOKEN` with at least 32
characters. `PORT` defaults to 8080. Production hosting must terminate HTTPS.
Enter the token in the page; it is kept in memory and sent as an Authorization
header. The dashboard displays actual persisted properties, tax verification,
run status, request usage and email outbox. Stale qualification is downgraded in
the display. `/healthz` checks the database/schema without exposing records.

## Production scanner

Start from `.env.example`; configure secrets only in the hosting environment.
`python -m rentalsourcing.cloud --migrate` applies additive migrations without
provider calls. `--status` shows DB state; `--resume JOB_ID` resumes stored work.
`--scheduled` gates to 08:00 America/New_York with DST support. See deployment
instructions before enabling providers or creating paid resources.

The collector reserves each attempt atomically before transport. Daily, billing
cycle and explicit approval limits are shared across processes. No automatic
retries are enabled by default. Response/tax caches and durable cursors prevent
unnecessary repeated calls. Keep usage accounting when migrating or restoring a
DB; a fresh DB is not a fresh authorization. Provider usage elsewhere is outside
the local ledger.

Email requires explicit activation, production configuration, Resend secret,
verified sender and recipient. A frozen outbox and provider idempotency protect
against duplicate delivery attempts; uncertain outcomes require reconciliation
when the safe retry window expires. No demo fallback exists in the live path.

Render hosts one dashboard web service, one scheduled collector and one shared
persistent PostgreSQL database. The Blueprint creates paid resources and must
not be applied without cost approval. No MLS/Zillow/Redfin feed is connected.
