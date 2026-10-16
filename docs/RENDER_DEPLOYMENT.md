# Render release handoff

Current launch instructions and costs: [LAUNCH_HE.md](LAUNCH_HE.md).
Live sample evidence: [LIVE_ACCEPTANCE_HE.md](LIVE_ACCEPTANCE_HE.md).

`render.yaml` defines paid Web Starter, paid cron, and paid persistent PostgreSQL
in Virginia. Baseline hosting estimate: $15.50/month before overages/taxes/domain.
This configuration does not authorize provisioning or incur charges by itself.
Both services explicitly deploy verified `master` with `autoDeployTrigger: checksPass`.
Builds verify the bundled official boundary offline; they never call RentCast.

Connect Render/GitHub and obtain explicit resource-cost approval before applying
the Blueprint. Provider flags remain disabled and allowance zero. Cron secrets:
RENTCAST_API_KEY, RESEND_API_KEY, EMAIL_FROM, EMAIL_TO. Sender must be verified.
Render links the same private PostgreSQL URL and generates DASHBOARD_PASSWORD;
DASHBOARD_USERNAME defaults to owner. HTTP routes never scan or send mail.

The four previous RentCast attempts are exhausted. Do not recreate their allowance
on Render. Preserve the original usage/history/outbox when moving live records:
`python -m rentalsourcing.transfer`, using private SOURCE_DATABASE_URL and
DATABASE_URL. It refuses a nonempty destination and copies transactionally under
scan locks. It sends no API requests or email. Do not commit data exports/secrets.

Check deployed /healthz, authenticated /readyz, dashboard and persisted records.
Only after explicit email approval run a safe test; provider acceptance and inbox
receipt are separate evidence. Enable 08:00 America/New_York cron only with a
separately approved ongoing API budget. Limited allowance leaves visible partial
coverage and Pending Verification; it does not prove a complete city scan.

A failed scan remains visible and resumable. Mail idempotency/outbox survive
restarts. Uncertain delivery beyond the safe retry window requires checking
Resend before manual reconciliation. Use provider database backups and test
restoration without resetting request counters or delivery history.
