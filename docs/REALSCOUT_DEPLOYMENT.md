# Production handover: staged activation and rollback

**Local implementation complete; production activation NOT authorized/performed.** Existing live emails continue independently. Do not push to master until approval: Render's existing auto-deploy may trigger on checks. No replacement infrastructure or second daily schedule is needed.

## Readiness

| Item | State |
|---|---|
| Maintained application integration and original criteria | Complete offline |
| Original Exclusives HTML + MJML + exact-link validation | Complete |
| Regular saved-search parser | Synthetic-tested; needs actual .eml validation |
| Signed webhook, persistent jobs, retries/recovery, encrypted links | Complete offline |
| Original outbox/daily idempotency and source-aware report | Complete offline |
| Strict selective API limits/cache reuse; no bulk in RealScout mode | Complete offline, disabled |
| Disposable PostgreSQL migrations/concurrent workers | Tested; never production |
| Receiving account eligibility/cost and raw-message availability / aligned DKIM preservation | Blocked on approved provider/account inspection |
| Gmail forwarding route incl. sender auth/body shape/confirmation | Blocked on permission and controlled receipt |
| Existing live Render/DB/schedule/provider status inspection | Blocked on secure access authorization/configured connector |
| Production backup, deployment/migration, controlled report send | Requires explicit approval |
| ROI/rental estimates | Unsupported evidence/assumptions; remain unknown |

## Existing deployment: do not create replacements

Use the existing Render web service, existing PostgreSQL DATABASE_URL, existing Resend sending key/domain/from/to, and existing daily cron. Do not apply the Blueprint to provision new services/database. Preserve the cron expression `0 5,6 * * *` and existing command `python -m rentalsourcing.cloud --scheduled-email`. Its Israel 08:00 guard remains. Existing web start `sh scripts/start_web.sh` migrates under lock then launches two Gunicorn workers. Build stays `pip install -r requirements.txt && python -m rentalsourcing.cloud --check`.

Before approved deployment, the operator must inspect the actual service/cron inventory and any VPS/systemd/GitHub jobs with secure read-only access, confirm only the intended existing scheduler sends, inspect the current outbox/rejected/uncertain messages, and take a provider-native DB backup. This environment has no Render/VPS/PostgreSQL/Resend account credential to verify live state. The checked-in Render manifest is evidence of intent, not proof of active deployment.

## Variables (set privately in Render, never chat/Git)

Keep all existing provider keys, EMAIL_FROM/EMAIL_TO, EMAIL_APPROVAL_ID, database/dashboard secrets and API counters/approval IDs unchanged. Shared RealScout configuration must match on web and cron.

| Variable | Staged value / purpose |
|---|---|
| DISCOVERY_SOURCE | `rentcast` until daily cutover; `realscout` only after approval |
| ENABLE_REALSCOUT_INGEST | `false`, later true for signed receiving endpoint |
| ENABLE_REALSCOUT_WORKER | `false`, later true after original raw DKIM path verified |
| ENABLE_REALSCOUT_DAILY | `false`, later true on web+existing cron for report adapter |
| ENABLE_REALSCOUT_ENRICHMENT | `false`; selective property evidence only after paid-call approval |
| ENABLE_REALSCOUT_STATUS_VERIFICATION | `false`; optional extra exact-address request, separate activation |
| REALSCOUT_PROCESSING_APPROVAL_ID | Recorded authorization for permitted automated alert processing |
| REALSCOUT_INBOX_PROFILES | JSON exact dedicated inbox/from/profile; example below |
| REALSCOUT_SAVED_SEARCH_VALIDATED | `false`; true only after actual regular template validated |
| RESEND_WEBHOOK_SECRET | Secret for this inbound endpoint, supplied by Resend (whsec_) |
| RESEND_RECEIVING_API_KEY | Receiving-read-capable API key; use narrowest provider-supported scope; never sending-only key by assumption |
| REALSCOUT_LINK_KEY | Fernet key generated securely by operator, backed up separately from DB; same on web+cron |
| PUBLIC_APP_URL | Actual existing HTTPS dashboard URL, no credentials/query/fragment |
| REALSCOUT_TRUSTED_AUTHSERV_ID | RETIRED; must remain blank |
| REALSCOUT_AUTH_CONTRACT_APPROVAL_ID | RETIRED; must remain blank |
| REALSCOUT_RAW_DOWNLOAD_HOSTS | JSON exact approved storage hosts; see REALSCOUT_AUTHENTICATION.md |
| REALSCOUT_WORKER_MAX_JOBS | `20` per drain |
| REALSCOUT_WORKER_MAX_ATTEMPTS | `5` before quarantine |
| REALSCOUT_WORKER_LEASE_SECONDS | `180` (>=90) |
| REALSCOUT_WORKER_POLL_SECONDS | `30` |

Example policy with placeholders, NOT real credentials/addresses:

```json
[
  {"inbox":"exclusive@YOUR-MANAGED-SUBDOMAIN.resend.app","allowed_senders":["EXACT-ORIGINAL-FROM@REALSCOUT-DOMAIN"],"profile":"exclusive_alert","original_recipients":["AUTHORIZED-ORIGINAL-GMAIL-ADDRESS"]}
]
```

Do not copy the placeholder sender. The exact From is recovered privately from the supplied original. Once a regular .eml is validated, add a **different alias** with `profile:"saved_search",search_name:"RentalSourcing - Pittsburgh Under $200K"`, exact sender and `REALSCOUT_SAVED_SEARCH_VALIDATED=true`. The two templates are not interchangeable. Never enable wildcard sender/domain matching.

Keep ENABLE_RENTCAST=false and ENABLE_EMAIL at its existing production value during dormant deployment. RealScout code does not change legacy behavior while flags/source remain defaults. During approved RealScout-only dry preview set ENABLE_EMAIL=false on the cron **only with authorization**, or perform isolated preview without modifying live flags. Do not disable existing daily email merely to test. Live feature-flag changes are production changes requiring approval.

Existing request caps still apply: APPROVED_API_REQUESTS/ID/mode, MAX_API_REQUESTS_PER_RUN/DAY/MONTH, TAX_CACHE_HOURS, NEGATIVE_TAX_CACHE_HOURS, LISTING_CACHE_MINUTES, NETWORK_RETRIES. No new paid plan assumed; no approval replenishment allowed implicitly. If exhausted, use caches or report pending. Do not set LISTING_REQUESTS_PER_DAY=0 (legacy config forbids it); RealScout mode handles listing reserve internally.

## Exact Gmail + Resend connection steps (not executed)

1. Confirm the agent/RealScout permits automatic private processing/forwarding of both alert types, recipient scope and retention. Confirm Resend receiving eligibility, limits/pricing and any Gmail Workspace forwarding restrictions; do not upgrade/add charges implicitly.
2. In the **existing** Resend account, use its receiving managed subdomain (shown by Receiving), or an expressly approved separate inbound subdomain. Choose dedicated `exclusive@…` and later `saved-search@…` aliases. A managed subdomain avoids changing MX for the current Gmail/sending domain. Do not replace primary-domain MX.
3. After approved dormant application deployment/migration, in Resend Webhooks add endpoint `https://EXISTING-APP/webhooks/resend/inbound`, subscribe only to `email.received`, securely install its signing secret and a receiving-capable API key in Render, provision/backup the Fernet key and exact profile policy. No message-body content goes in webhook URLs or logs.
4. In Gmail web Settings -> See all settings -> Forwarding and POP/IMAP -> Add a forwarding address, enter the dedicated alias. Gmail sends a confirmation message to it. With authorization, use the provider receiving dashboard to complete this confirmation or have the owner click/enter the confirmation. Do **not** allowlist Google confirmation senders in the property parser: these messages are not listings. If the signed webhook rejects them, use the receiving dashboard to read the confirmation independently. Leave global 'forward all mail' disabled.
5. Gmail Settings -> Filters and Blocked Addresses -> Create a new filter: exact original From address (not broad @domain), and exclusive subject `Exclusives: Be the first to see these homes`. Preview search results and ensure they only include authorized alerts. Create filter -> Forward it to the exclusive alias. Do not tick 'also apply filter to matching conversations' (no bulk private historic forwarding). No Gmail OAuth/password/IMAP grant is required. Gmail may require a separate forwarding address and confirmation for the regular alias.
6. Regular template: after original sample validation, create a separate narrow filter using its exact From plus actual subject/search-name matching supported by the sample; forward to the saved-search alias. Do not invent this subject from the Exclusives sample. Gmail subject/from filtering cannot grant blanket permission to parse unrelated templates.
7. With explicit authorization for **one controlled receipt**, follow REALSCOUT_AUTHENTICATION.md: obtain one raw message using the official SDK-supported raw URL, approve the exact storage host privately, verify the original aligned DKIM and signed original-recipient headers, and confirm the signed webhook destination. No Authentication-Results or received_for trust bypass is permitted. Leave the retired header-trust variables empty; require REALSCOUT_RAW_DOWNLOAD_HOSTS and original_recipients. Missing raw bytes/altered signed content quarantine. Do not enable processing merely by recording an approval ID.
8. Enable INGEST+WORKER with approval and validated settings, inspect private `/api/realscout/status` and `/api/realscout/report`. Jobs persist across restarts. Requeue approved quarantined messages only after the cause is fixed. Confirm one source message produces one observation per card on duplicate webhook delivery and no portal link is opened. Keep paid enrichment off for this transport validation.
9. With separate production/report approval, set DISCOVERY_SOURCE=realscout and DAILY=true on existing web/cron using one configuration release. Keep existing cron and existing verified sender/recipient configuration. Inspect prepared report before first controlled send; preserve outbox sent/uncertain history. If a legacy report already sent today, the RealScout report waits until next day instead of duplicating it. Selective RentCast/status remain off until separate bounded spending approval.

## Database and migrations

`python -m rentalsourcing.cloud --migrate` is the existing migration entry point, also run by start_web.sh. Use only after backup and deployment/migration approval. Changes are additive and idempotent in the existing database; do not export/replace/truncate production.

New tables: realscout_messages, realscout_candidates, realscout_observations, realscout_links, realscout_inbound_jobs, realscout_evidence, realscout_status_evidence, realscout_reports, realscout_report_items, realscout_health, realscout_migrations; version2 adds realscout_dkim_keys and realscout_auth_evidence. Queue lease/retry/profile fields and message processed_at are additive. Existing schema_versions, properties/history/taxes/cache/usage/approval/outbox and sender errors remain. Migration marker version1 is separate from original schema version2. Per-operation transactions plus PG advisory locks preserve atomicity. No down migration drops history. Transfer utility preserves new tables when source/target share the migrated schema; do not run it against production to activate this integration.

## Monitoring and recovery (operator actions)

- Public `/healthz` checks base service/DB readiness; it is not proof email ingestion is healthy. Monitor private `/api/realscout/status`: pending/retry/processing/quarantined counts, last heartbeat, oldest queue entry. Queue >1 hour and heartbeat >5 minutes require attention. It exposes no bodies/links/sender credentials.
- CLI `python -m rentalsourcing.cloud --realscout-status` is read-only. `--realscout-worker-once` retrieves approved queued email bodies (requires existing authorization; no property calls/sends). `--realscout-requeue UUID` resets only a quarantined/retry email job after repair, never spending counters. Failed templates/authentication require review, not infinite retry. Keep provider receive retention long enough for delayed retries; retention/account guarantees remain unverified.
- Worker restarts recover expired leases; ingestion-before-ACK crash replays idempotently. Provider 404/delivery delay, 429/network/5xx retry with bounded backoff; unsupported/auth/oversize messages quarantine. Persistent DB failure gives webhook503 so provider retries. If downtime exceeds provider webhook/body retention, inventory receiving dashboard with explicit access approval and reconcile approved messages; no automatic mailbox-wide backfill exists.
- Existing sender: inspect outbox and sanitized rejection with `--inspect-email ID`; inspect provider delivery status only with approved account access (`--email-status ID` makes a live provider request). Wrong sender/recipient must be corrected/verified in Resend before reactivation. An uncertain send is not retried beyond the original 23-hour safe window; reconcile from actual provider acceptance before marking sent. Do not reset daily keys or create a second cron to force a send. Current live Resend sender/recipient issue cannot be confirmed fixed from local code/tests.
- Logs contain event/category/counters, not email bodies/IDs/private links/API secrets. Keep provider dashboards/access restricted; use existing backup/retention policy, do not blindly delete dedup/usage/outbox history. No new paid monitoring service is provisioned.

## Rollback

Before deployment retain base revision and private environment snapshot, DB backup and link-key backup. If receiving fails, disable WORKER+INGEST (approved operational change); Resend will need dashboard review/replay for notifications arriving while disabled. Keep queue/history/ciphertexts/counters. Do not delete tables or change encryption key.

If RealScout daily cutover fails, restore the previous daily/source flags as one reviewed configuration change and redeploy the previous known-good code if necessary. Preserve existing cron command/expression and same DB. **Do not re-enable RentCast bulk collection or paid caps unless the previous configuration explicitly authorized them.** An already-sent daily outbox remains sent; do not overwrite it for a second delivery. Failed/uncertain provider attempts require existing reconciliation procedures. Additive tables can remain during code rollback; they do not alter legacy inventory or quotas.

## Only owner-required actions

1. Confirm permitted automated RealScout alert processing/forwarding and approve the controlled receiving validation.
2. Supply one original **regular** saved-search .eml; no password/key in chat. Exclusives sample is already validated.
3. Grant secure authorized access to existing Render/Resend/VPS settings, or provision the private env values through their dashboards; confirm Gmail's forwarding-address verification when required. Provider support must establish the raw-message availability / aligned DKIM preservation/receiving eligibility before activation.
4. Explicitly approve final production deployment/migration and controlled real email send; separately approve any bounded paid RentCast allowance if wanted. No technical commands are required from the owner.

Authentication update: REALSCOUT_AUTHENTICATION.md supersedes all earlier header-contract guidance. No services or flags were activated.
