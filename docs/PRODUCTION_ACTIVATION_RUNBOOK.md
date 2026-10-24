# Controlled activation and rollback — existing Render services

Status: engineering validated offline; NO merge, deployment, production query, migration, environment edit, mailbox operation, provider request or real email performed. MLS licensing correspondence is already underway; no further vendor research/contact or brokerage involvement is needed from engineering.

## Integration audit

PR #10 branch `codex/multisource-mls-20261024` contains PR #9 head `bc2b3e4` as an ancestor. Production master remains `051e11a`. PR #10 is stacked on PR #9, not yet targeted at master. There are no competing replacement apps or duplicate delivery cron jobs. RealScout queue/parser/authentication remain the PR #9 implementation; unified ingestion reads verified stored candidates. PR #10 adds independent `discovery_*` tables; it does not replace legacy properties/history/cache/quota/outbox. Tax/geography thresholds are unchanged. A stored-data report and unified report use the SAME `daily:<Israel-date>` outbox key.

Audit fixes: mismatched source-kind/transport and typo/invalid financial inputs fail validation rather than silently skipping; rent/HOA conflicts prevent qualification; estimated tax cannot cause a hard tax rejection; fractional tax years are rejected; cash-on-cash with zero initial equity is explicitly incomplete; scan state and readable HTML reporting are improved. Previously fixed mixed lease/sale handling and PR #9 cryptographic checks remain intact.

## Why current emails can contain no fresh scan

Repository evidence, not a live account/log diagnosis: Render's cron command is `python -m rentalsourcing.cloud --scheduled-email`. With existing DISCOVERY_SOURCE=rentcast, cloud.py calls Digest.prepare_stored and Digest.send; it does not invoke Engine.run or a listing collector. Thus a daily email can correctly report no fresh scan even when API usage remains. The Blueprint's API approval defaults0, monthly internal ceiling40, and cron ENABLE_RENTCAST=false also prevent unapproved collection. Actual live overrides/current cycle consumption and current Resend sender status were NOT inspected. Do not infer that this code audit proves which live quota was consumed or that a historic Resend error is still active.

Future DISCOVERY_SOURCE=unified plus explicit flags routes the SAME command through discovery.runtime.daily before report preparation. It reports fresh discovery independently of whether new entities were found; cache-only reuse, skipped cadence, no alerts, unavailable source, partial scan, internal budget exhaustion and pending finance are distinct. No source operation recorded means no scan claim. A fresh one-page RentCast response is not an inventory-completeness claim.

## Release order — perform ONLY after production approval

1. **Record/backup:** operator verifies web `rentalsourcing-dashboard`, cron `rentalsourcing-daily`, database `rentalsourcing-db`, deployed SHAs and current secrets/configuration without exporting secrets to GitHub/chat. Obtain a verified database backup/restore point and inventory current outbox rows, counts/history/caches/usage and latest accepted Resend message ID. Identify uncertain/attempted deliveries; do not delete or reset them.
2. **Control auto-deploy:** since master auto-deploys, the approved release operator temporarily selects manual deployment for BOTH existing services before any merge. This is an explicitly authorized future production setting change, not performed here. Keep the existing schedule and email settings unchanged. Do not apply the Blueprint: it could override working email flags and describes existing paid resources.
3. **One reviewed release:** when authorized, retarget PR #10 onto master so it includes reviewed PR #9 changes, confirm checks and merge the combined release once. Do not independently merge PR #9 first while auto-deploy is active. PR #9 remains available for its security review; avoid duplicate cherry-picks. No retarget or merge has been done.
4. **Dormant web first:** retain existing DISCOVERY_SOURCE and email/API approval values; keep every new flag below false. Manually deploy the approved SHA to the EXISTING dashboard. Existing startup `sh scripts/start_web.sh` runs `python -m rentalsourcing.cloud --migrate`, then Gunicorn with2 workers. Startup migration uses the existing scan lock; if a cron holds it, migration aborts rather than running concurrently. Retry the deployment after the current job finishes; do not schedule another migration job. Build `--check` performs no API calls.
5. **Validate web/database:** health200, private routes require Basic Auth, disabled RealScout/MLS webhooks404. Confirm additive schema markers, unchanged legacy rows/history/counters/outbox and no receiving worker or provider request. `Store.migrate()` runs legacy v2 then RealScout then unified migrations; each stage is repeatable, and failure is recovered by retrying startup. Do not roll back with DROP statements. Existing startup criteria reevaluation is preserved and is not a new source scan.
6. **Dormant cron second:** deploy the SAME SHA to the existing cron, retaining its command, schedule, recipient/sender and current source selection. Observe one existing daily report for unchanged behavior; no second daily job or test email is required. Restore the prior auto-deploy policy only as part of the approved release record.
7. **Controlled ingress/preview:** after separate permission, configure exactly ONE authorized source on the web service; keep daily delivery and paid enrichment off. For a licensed MLS feed, securely configure scoped credentials and approved metadata/system identifier. For RealScout, apply PR #9 exact recipient/sender/key/cryptographic settings and one approved forwarded message. Authentication failure remains a rejection. A regular-alert sample is still required; do not bypass its gate. Use source-specific configuration references, never shared portal credentials. A CSV endpoint requires its source-bound signing secret, timestamp and nonce.
8. **Cutover:** only after an approved ingestion/dry report and costs/account baseline are verified, configure unified source profiles and assumptions on the cron, set DISCOVERY_SOURCE=unified, ENABLE_UNIFIED_DISCOVERY=true and ENABLE_UNIFIED_DAILY=true. The web needs ENABLE_UNIFIED_DISCOVERY=true only for its private report; it does NOT need daily or RentCast flags. Activate only explicitly approved source profiles. Existing cron remains `0 5,6 * * *`, guarded to08:00 Israel (05UTC summer,06UTC winter). Activate after a day's existing report if appropriate; a sent daily outbox prevents another report/scan that day. The next scheduled day runs discovery. Never alter the day/idempotency key to force a second email.
9. **Acceptance/monitor:** with separate live-request/delivery approval, verify one run's actual source work, source timestamps, outbox state/provider ID, request counters, missing-data reasons and protected listing links. No-new-listings is a valid outcome; skipped/no-access is not a successful scan. Check Basic-Auth `/api/discovery/report` and existing cron logs for stale sources, failures, last successful data check and pending/uncertain delivery. `/healthz` alone does not prove cron/source health. Reconcile an uncertain message before an unsafe late retry.

## Dormant flags — BOTH services unless specifically needed later

```
ENABLE_UNIFIED_DISCOVERY=false
ENABLE_UNIFIED_DAILY=false
ENABLE_MLS_EXPORT_WEBHOOK=false
ENABLE_UNIFIED_RENTCAST_DISCOVERY=false
ENABLE_UNIFIED_RENTCAST_ENRICHMENT=false
ENABLE_UNIFIED_STATUS_VERIFICATION=false
ENABLE_UNIFIED_RENT_ESTIMATES=false
ENABLE_REALSCOUT_INGEST=false
ENABLE_REALSCOUT_WORKER=false
ENABLE_REALSCOUT_DAILY=false
ENABLE_REALSCOUT_ENRICHMENT=false
ENABLE_REALSCOUT_STATUS_VERIFICATION=false
REALSCOUT_SAVED_SEARCH_VALIDATED=false
```

Do not change existing ENABLE_EMAIL, ENABLE_RENTCAST, DISCOVERY_SOURCE, recipient/sender or API approval values during dormant deployment. New disabled behavior cannot initiate paid requests; this does not assert that unrelated already-authorized legacy jobs stop consuming requests. No new credentials are needed to deploy dormant code.

When approved, use existing DATABASE_URL, RESEND_API_KEY, EMAIL_FROM/TO, EMAIL_APPROVAL_ID, APP_ENV, dashboard auth, boundary file and PUBLIC_APP_URL. Additional unified profiles/approvals/assumptions are documented in MULTISOURCE_INTEGRATION.md and the fallback template. Trestle secrets TRESTLE_CLIENT_ID/SECRET belong in Render secrets on the service performing the approved query; actual expected_system comes from scoped metadata. Keep all provider secrets out of JSON files. RealScout settings and inbound route `/webhooks/resend/inbound` remain documented in REALSCOUT_DEPLOYMENT.md.

## Exact fallback proposal — NOT activated or approved

`docs/examples/rentcast-fallback.pending.env` is a preparation template, not an executable production configuration. It deliberately leaves ENABLE_RENTCAST=false and approval0. Activating it requires a new explicit approval ID, known real billing anchor and account usage baseline, and ENABLE_EMAIL must remain at its existing working value. Do not copy placeholder IDs as permission.

For a31-day billing cycle, one500-result page/day consumes31 requests. Allocate3 enrichment attempts (tax and rent share this pool) and6 verification attempts:40 total,10 below the included50. All retries/failures conservatively consume these allocations. This supports at most one fully uncached two-call underwriting plus one extra enrichment attempt; it cannot financially verify a large daily cohort. Cache hits can help. The current8-call discovery allocation is incompatible with daily scanning and must be explicitly reallocated, never silently changed. Existing per-run/day4 remains adequate for a one-page scan plus selected enrichment; later candidates stop at the remaining budget.

Alternative16 discovery +18 enrichment +6 verification=40 can serve up to9 two-call candidates but sacrifices daily freshness. Keep daily discovery as the proposed priority, disclose limited verification, and make no coverage guarantee:500 results may truncate the cohort, city labels may miss municipal addresses and API completeness/lastSeen ordering are not MLS equivalence. Current-cycle baseline reduces available capacity. No automatic upgrade or overage is allowed.

## Rollback — preserve data and idempotency

If activation misbehaves, stop new behavior by disabling relevant new flags and restore the recorded DISCOVERY_SOURCE/configuration. Do not change the cron schedule. If no message was attempted, a pending unified draft may be reviewed/replaced through the existing outbox workflow. If a message was attempted/uncertain/sent, freeze its payload and reconcile using the actual Resend record before retry or legacy fallback; a rollback is not permission for duplicate mail. Preserve the Fernet link key and all request counters/caches/approval IDs.

Redeploy the recorded previous web SHA first if necessary; startup/storage compatibility was exercised against master. Redeploy the matching old cron SHA without reapplying Blueprint defaults. Leave additive tables in place; no down migration or restoration of an older DB snapshot over newer outbox/accounting is appropriate. Restore a backup only for a separately diagnosed data loss incident with explicit approval and post-backup delivery/quota reconciliation.

## Licensed MLS activation checklist

- Written license covers this independent use, storage/derivatives/private email, retention and hosting; total fees approved.
- Secure scoped credentials; inspect actual metadata, system identifier, status/removal rules, allowed fields and bootstrap size. Keep connector/source disabled until this review.
- Approved bounded test into isolated schema: real Pittsburgh addresses/units, status, tax/year, HOA, price and links normalize correctly.
- Second approved retrieval proves incremental changes, price reductions, out-of-eligibility status updates and removal handling. Property paging/checkpoints tested offline; full replication and deletions still depend on the contracted feed.
- Authorized matching reference snapshots measure real coverage/delay and gaps; no synthetic percentages count.
- Approved production backup/release, controlled ingest/preview, then same existing cron cutover and one authorized delivery acceptance. Passing tests or a successful API response alone is not CONNECTED.

## Blockers even after MLS approval

Live metadata/removal semantics and coverage acceptance; production backup/configuration/outbox inspection; explicit deployment/migration and live test authorization; approved rent/expense/financing assumptions and any missing profitability cutoff; correct provider billing baseline; verified sender/recipient configuration for Resend; RealScout forwarding permission/raw-DKIM acceptance and real regular-alert sample if that source is activated. A quote/assumption cannot be replaced with a guessed default. PostgreSQL restart and fake delivery tests do not establish live Resend delivery.
