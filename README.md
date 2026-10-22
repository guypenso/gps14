# RentalSourcing

Current launch instructions: [מדריך הפעלה מעודכן](docs/LAUNCH_HE.md).
Live acceptance status: [בדיקת החיבור האמיתית](docs/LIVE_ACCEPTANCE_HE.md).

The existing pipeline now includes a private, read-only Flask/Gunicorn dashboard.
It never starts scans or sends messages from HTTP routes. Render provisions Web + Cron + PostgreSQL
only after approval. Estimated baseline hosting is $15.50/month, excluding provider overages/taxes.

Pittsburgh investment-property collector, built on the existing RentalSourcing repository and RentCast integration.

The production entry point is `python -m rentalsourcing.cloud`. Provider traffic is disabled by default.
No RentCast call is possible on a cache miss until both the enable flag and a persistent user-approved
request allowance are configured. No paid services are provisioned by installing or testing this repository.

Current acceptance: 100 real listings, 58 rejected, 39 pending tax, three fully verified; all four authorized API attempts consumed. No cloud deployment or real email. See launch/acceptance links above.

## What runs

1. Fetch Active Sale Listings using early server-side filters and pages of up to 500 records.
2. Persist every observed record, including missing-field records as **Pending Verification**.
3. Verify the actual city polygon, excluded ZIPs, attributes and price. Known failures are Rejected.
4. Prioritize price reductions, reuse durable tax cache, and fetch property tax history only when needed.
5. Require `Latest Available Annual Tax + $100 < $2,000`. Missing latest-year amount stays Pending Verification.
6. Persist one daily Resend digest containing new verified properties or newly observed price reductions.
7. Save cursors, tasks, observations, price revisions, tax history, configuration, quota usage and delivery state in PostgreSQL.

All criteria remain fixed: inside Pittsburgh PA, Active/For Sale, bedrooms >=3, bathrooms >=1,
living area >=1,000 sqft, price <=$200,000, ZIP 15210 and 15203 excluded. There is no demo fallback.

## Quick local validation

Python 3.12 is the production version. Use an isolated virtual environment:

```sh
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

Without `TEST_DATABASE_URL`, PostgreSQL tests are explicitly skipped. To run them, provide a local disposable
PostgreSQL database; tests create and drop isolated schemas. **Never point TEST_DATABASE_URL at production.**
No real RentCast or Resend requests occur in the tests: the default HTTP opener is blocked and transports
are controlled test doubles. PostgreSQL 16 integration was also exercised in the development environment.

```sh
TEST_DATABASE_URL=postgresql://postgres:local-tests-only@127.0.0.1:55432/rentalsourcing .venv/bin/python -m pytest -q
```

## Production geometry

`python scripts/fetch_boundary.py` downloads the City of Pittsburgh's official CityBoundary layer linked
from WPRDC. It requests WGS84, checks polygon topology and representative inside/outside points, and records
the source, retrieval time and SHA256. The production loader validates the manifest and checksum.
The build stops if this source cannot be fetched. Test polygons never substitute for production geography.

## Render deployment

`render.yaml` provisions one paid Web service, one paid Cron job and one paid PostgreSQL instance **only when the user approves and
applies the Blueprint**. Its build installs pinned dependencies, downloads/verifies the boundary and runs
an offline preflight. Runtime migrations are additive and keep initial-release tables intact.
Do not copy Codex proxy/CA settings to Render. Normal verified HTTPS uses the runtime's own proxy/CA configuration.

Read [the Hebrew activation guide](CLOUD_GUIDE_HE.md) and [the current readiness report](READINESS_HE.md).
The configured UTC schedule is `0 12,13 * * *`; `--scheduled` only executes the slot corresponding to 08:00
America/New_York. A complete scheduled job is skipped on repeat; a failed/partial job can be resumed.
Render scheduling can be delayed; this is an intended start time, not a second-level timing guarantee.

## Commands

```sh
python -m rentalsourcing.cloud --check            # Offline config/geometry preflight, no DB/provider requests
python -m rentalsourcing.cloud --migrate          # PostgreSQL migrations only
python -m rentalsourcing.cloud --status           # DB status, budgets, pending verification and outbox
python -m rentalsourcing.cloud                   # Manual scan; prints its resumable job ID
python -m rentalsourcing.cloud --scheduled       # 08:00 Pittsburgh guard
python -m rentalsourcing.cloud --resume JOB_ID   # Resume cursor/tasks; no successful pages repeated
python -m rentalsourcing.cloud --retry-email ID  # Same frozen body/key, within 23h safety window
python -m rentalsourcing.cloud --reconcile-email ID --provider-message-id ACTUAL_RESEND_ID
```

Reconciliation is a manual record of acceptance verified in Resend; it does not send another message.
Only one digest is prepared per local day, including for manual runs. Events arriving after that day's
message remain eligible for the next day's digest after a fresh listing verification.

## Allowances, cache and billing

Copy `.env.example` into private runtime settings, never commit actual values. `API_APPROVAL_ID` identifies
an explicit approval; `APPROVED_API_REQUESTS` is the cumulative cap for that approval and does not reset.
There are also per-run, UTC-day and billing-cycle caps. Set BILLING_DAY to the account's cycle day (1–28;
for a later date use 28 conservatively and retain the cumulative cap). All attempts consume the app allowance,
including failures and optional retries. Other applications using the same provider key are outside this counter.

Defaults: approved allowance **0**, 4 attempts/run, 4/day, 40/billing cycle, no automatic network retries.
Scheduled runs limit themselves to a fair share of the remaining allowance so early tax checks do not
consume the entire month. Forecast shortfalls and 85% usage are saved to operations and structured logs,
and appear in the daily email when email is enabled. Exhaustion pauses the job safely; it never raises
spending limits automatically. The default trial budget does not guarantee full daily city coverage.

Listing-page cache: 15 minutes. Verified annual tax cache: 7 days, refreshed on entering a new year.
Missing/invalid tax amounts have a shorter 6-hour cache so pending properties can become verifiable.
Listings older than 24h are not sent. Tax refresh can be skipped for an already-notified property with no
new price reduction; it is retained as Pending Verification if its old tax cache expired.

A single digest uses a durable outbox and Resend's idempotency key. Acceptance updates outbox and event
states atomically. A timeout remains `uncertain`; resuming within 23h uses the identical key/body.
After that window, the system requires reconciliation instead of risking a duplicate send. A stale
never-attempted digest is cancelled. Provider acceptance is not proof of inbox delivery.

## Operational limitations

A full query is not proof of every listing in the city: RentCast coverage and postal-city classification
still apply. Tax year means latest available reported annual year, not a prediction of the next tax bill.
Offset pagination can shift while provider listings change. No MLS/Zillow/Redfin connector is implemented.
The default discovery strategy remains RentCast; the separately gated RealScout adapter is described below. Credentials, live provider acceptance and Render
billing approval remain deployment prerequisites; no live-provider success is claimed by offline tests.

## Controlled proof of concept

`python -m rentalsourcing.poc` prints the exact URL-encoded 100-listing request plan without any network
or DB connection. After separate allowance approval, `--execute` reuses the existing collector/engine
for one page and at most three unique tax candidates. It never sends emails or fetches a second page.
See [the Hebrew POC notes](docs/POC_HE.md) for County-data limitations and the unmeasured coverage policy.

## Dashboard

Set DATABASE_URL and a private DASHBOARD_PASSWORD of at least 20 characters.
Run `sh scripts/start_web.sh`; Render supplies PORT. Username defaults to owner.
GET /healthz is a database health probe; /readyz and all data require HTTP Basic authentication
over the platform HTTPS URL. /api/dashboard exposes only stored live-source observations;
stale/missing mandatory data cannot be displayed as qualified. /api/email-preview never sends.
Do not expose a localhost test server as a public production service.

API_APPROVAL_MODE defaults to one_time. Optional rolling_32_days requires an explicit recurring
allowance approval and counts all app attempts across the preceding 32 UTC dates, preventing a
calendar reset from creating a new spending allowance. No mode monitors other applications using
the provider account. Existing four-call approval remains a one-time allowance.


## RealScout email discovery (disabled by default)

The maintained app now includes profile-specific MIME/HTML/plain-text RealScout parsing, signed
Resend inbound jobs, a restart-safe PostgreSQL worker, cached/selective financial evidence and
source-aware reports through the existing daily outbox. No new cron or database is needed.
The original Exclusives sample is validated; a real saved-search sample and one controlled
raw-message/forwarding DKIM compatibility test remain activation gates. All RealScout flags default off.

See [implementation and criteria](docs/REALSCOUT_INGESTION.md),
[exact configuration, Gmail/Resend setup, migration and rollback](docs/REALSCOUT_DEPLOYMENT.md),
[actual sample validation](docs/REALSCOUT_SAMPLE_VALIDATION.md), and
[offline test results](docs/REALSCOUT_TEST_RESULTS.md).
The [synthetic report](docs/examples/realscout-synthetic/report.txt) is explicitly fictional.
`python scripts/realscout_dry_run.py --synthetic --output-dir /tmp/realscout-preview` runs offline
through the maintained modules without provider calls, email sends or persistent state.

Sender-authentication implementation and verified provider findings: [original raw DKIM verification](docs/REALSCOUT_AUTHENTICATION.md). No textual Authentication-Results waiver is supported.
