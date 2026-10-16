# Render deployment handoff

`render.yaml` defines one free web dashboard, one paid cron job, and paid persistent PostgreSQL in Virginia. It is a deployment configuration, not evidence of a deployment or authorization to incur costs. Review current Render prices and approve them before applying the Blueprint. Free web instances can sleep and take time to wake; choose a paid web plan only with separate approval if immediate availability is required.

The cron and dashboard use the same PostgreSQL database through Render's internal connection string. The database accepts no public IPs. The web process receives no RentCast or Resend key. Render generates a secret `DASHBOARD_TOKEN`; retrieve it from the service environment and use it in the dashboard sign-in form. Custom tokens must contain at least 32 characters. Render supplies `PORT` and HTTPS. `/healthz` is the platform health endpoint; property data requires authentication.

Before deployment, run:

```sh
python -m pip install -r requirements-dev.txt
python -m pytest -q
python -m rentalsourcing.cloud --check
```

The GitHub Actions workflow also supplies PostgreSQL 16 to tests through `TEST_DATABASE_URL`. Local runs without that URL skip database integration tests. Never point integration tests at a production database.

After approved resource creation, set the cron's RentCast and Resend credentials, verified sender, and recipient using Render secrets. Leave both enable flags `false` until live acceptance and real email sending are authorized. The web startup command runs the existing additive `--migrate` command before starting the dashboard, without calling either provider. The dashboard can run while scanning and email remain disabled. Verify its health endpoint and authenticated dashboard against the persistent database before enabling the cron.

New deployment defaults have **zero approved RentCast attempts** (`APPROVED_API_REQUESTS=0`, empty `API_APPROVAL_ID`) and both enable flags disabled. Current authorization permits **four total attempts** across listing and tax calls, including failures; it does not authorize a new four-attempt allowance per deployment. Set an immutable approval ID and approved count only after accounting for attempts already consumed under this authorization. Daily and monthly caps are four as an additional guard. Retain the persistent approval ledger; a new database or restoring an earlier backup can lose usage accounting. Previously spent attempts must be carried forward before enabling a new environment. Check provider usage outside this application as well. Do not change approval IDs to reset the allowance, raise approved counts, or enable paid overages without approval. Four attempts permit a bounded acceptance run, not indefinite daily scans.

The schedule fires at 12:00 and 13:00 UTC. Application gating permits only 08:00 America/New_York and persisted daily scan state prevents duplicate processing. Check logs and the dashboard after the first approved run; failed scans can resume persisted cursors and verification tasks within the approved budget. Confirm actual listing, boundary, and tax outcomes before approving real email delivery. Do not interpret an empty dashboard as proof that a successful scan found no deals.

For acceptance, record the deployed commit, database connection result, boundary provenance verification, run summary, persistent request usage, property/tax results, and (only with approval) provider email receipt. Preserve database backups and test restoring them without resetting the request ledger. No Render resource, external mail, or purchase is created by this document or the test workflow.

The 2026-10-16 live acceptance run has now consumed all FOUR authorized attempts (one listing page + three tax records). Do not grant this allowance again on Render. The bundled verified official boundary is checked offline during the cron build; refreshing it is a deliberate maintenance action, not a build dependency.
