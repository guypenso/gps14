# Multi-source integration — 2026-10-24

## Financial-policy update

New unified reports use dual-v1 cash/financed comparisons and separate balanced rankings; see DUAL_UNDERWRITING.md. Earlier single-mode financial/score descriptions below are historical compatibility behavior, not the owner's new comparison policy. No proposed expense or loan terms are auto-approved.

## Release boundary

This change extends the maintained application and depends on draft PR #9. It is not a replacement application. All new behavior defaults off. No production deployment, migration, schedule, mailbox, paid request or live delivery was performed. MLS connectors are fixture-tested; **no authorized live MLS connection or real coverage measurement exists yet**.

## Independent access decision

The agent-sponsored route is superseded by the owner's independent-access requirement. See MLS_ACCESS_RESEARCH.md: direct West Penn private-use eligibility is BLOCKED PENDING LICENSING; Trestle is a delivery mechanism, not a license bypass. No broker sponsorship/export is proposed for activation.

## Recovered policy (unchanged)

The original evaluator remains authoritative: Pittsburgh municipal polygon, Pennsylvania, Active sale listings, price $0.01–$200,000, at least 3 bedrooms, 1 bathroom and 1,000 square feet; exclude ZIPs 15203, 15204, 15210. Latest annual property tax **plus $100 must be strictly below $2,000**. $1,900 annual tax fails. Missing latest-year tax does not fall back to an older bill.

No approved property-type exclusions, additional neighborhood exceptions, profitability cutoff, insurance, renovation, financing or reserve assumptions were recovered. No such policy is invented. Unknown critical facts, stale listing status, conflicting evidence or incomplete underwriting remain Pending Verification. A complete model passing the recovered filters is labeled Qualified, but that is not a guarantee of profitability or a claim every input is independently verified. Third-party rent estimates remain estimates. Investment score is annual net yield percentage points, not a calibrated 0–100 recommendation probability; legacy scoring is unchanged.

## Implemented flow

Authorized RESO/Trestle, signed CSV exports, authenticated RealScout history, optional RentCast and public parcel records feed additive `discovery_*` tables in the existing PostgreSQL database. Canonical address plus unit links sources; source listing keys remain separate. Conservative exact normalization deliberately avoids uncertain fuzzy merges. Immutable observations preserve provenance, source modification time, fetch/export time, price changes, conflicts and history. Newer weak data cannot silently replace authoritative evidence. Tax amount and year are atomic. Evidence freshness: price/status 24h, financial 7d, physical 31d. These are explicit engineering freshness limits, not investment thresholds.

Original properties, tax history, request counters, caches, RealScout queue, encrypted listing links and outbox remain intact. Existing daily outbox key/idempotency is reused. No new cron or paid service. The existing cron remains `0 5,6 * * *`, guarded to 08:00 Israel time. Unchanged listings do not become new merely because they were fetched again. Reports distinguish new, reductions, other changes, pending, newly qualified and previously seen; source results distinguish cache reuse, partial scans, wire calls and failures. A sent daily outbox blocks rescanning/resending that day. Attempted uncertain deliveries freeze content and retain existing reconciliation rules.

## Connector contract

`Source`: name, kind, permission_id, scope, enabled, authoritative, synthetic. Trust/scope cannot silently change after registration. `Listing`: source, record_id, street, city, state, ZIP, observed_at, fields, MLS ID/system, URL, modified_at, estimates, deletion flag, comparables. No enabled source without a permission reference.

RESO minimal mapping: ListingKey, ListingId, OriginatingSystemName, full address/unit, City, StateOrProvince, PostalCode, StandardStatus, ListPrice, BedroomsTotal, Bathroom totals or full/half counts, LivingArea, Latitude, Longitude, PropertyType/SubType, ModificationTimestamp, TaxAnnualAmount, TaxYear, AssociationFee/Frequency, ParcelNumber and listing URL. DOM is useful but not a hard filter. Missing values stay missing. Lease records are not purchase opportunities. HOA period must be known before monthly conversion.

RESO uses OAuth client credentials or bearer token, same-origin bounded pagination, durable checkpoints, a five-minute overlap and incremental watermarks. Incomplete pagination never claims complete coverage. Only same-origin Property URLs are followed. Up to three stale known listing keys can be rechecked per run by default. Actual licensed schema, scope, deletion semantics and feed limits require metadata/live acceptance before enablement.

CSV has the same RESO column names, UTF-8, unique headers, explicit export snapshot timestamp. File mode requires a producer manifest `{source, exported_at, sha256}`; file mtime is never freshness evidence. POST `/webhooks/mls/export/<source>` requires enabled source, HMAC SHA256, fresh timestamp and durable nonce. Signing bytes: `source + '.' + timestamp + '.' + nonce + '.' + exported_at + '.' + raw_csv`. Headers: `X-MLS-Timestamp` (Unix seconds), `X-MLS-Event` (unique nonce), `X-MLS-Exported-At` (UTC ISO timestamp), `X-MLS-Signature` (hex digest). Per-source secret must be at least 32 characters. Reused nonce/body returns idempotently; changed body or cross-source replay fails. The authorized producer must implement signing; no assumption an MLS portal natively supports it.

WPRDC exact parcel lookup supplies characteristics only. COUNTYTOTAL/LOCALTOTAL are assessments, not tax bills; SALEPRICE is historical, not asking price. No owner data is retained. This is not a listing discovery source. Adapter tested with fixtures, not real parcel requests.

RealScout bridge consumes only existing authenticated candidate records. Original DKIM verification and exact recipient gates remain. Regular saved-search fixture is synthetic and still requires a real authorized sample before removing its template gate. Original supplied Exclusives sample stays private and is not committed. Portal links are encrypted and opened through existing authenticated application routes; no portal scraping.

## Financial model and budget

Required: rent, latest annual tax, insurance, HOA, vacancy, maintenance, CapEx, management, closing costs and renovation budget. Assumptions require an explicit approval ID. Gross yield = annual rent / price. NOI subtracts vacancy, tax, insurance, HOA, maintenance and management. CapEx is deducted after NOI. Net yield = after-reserve annual income / (price + closing + renovation). Debt and cash-on-cash are calculated only for explicitly configured cash or loan scenarios. Loan assumptions must be supplied; no default interest/down payment. Completeness, provenance confidence and investment attractiveness are separate; confidence is a reproducible indicator, not a statistical probability.

Persistent request accounting wraps the existing locked reservation before transport. Cache hits cost zero. All attempted calls/retries conservatively consume internal allowance even though RentCast documents billing only HTTP 200. Original counters and approvals remain effective; known billing anchor and complete current account usage baseline are mandatory. Baseline applies only to its billing cycle. Hard application cap cannot exceed 50. Default internal cap stays 40, purpose caps 8 discovery / 22 enrichment including rent / 10 verification reserve. Other purposes cannot spend the unused verification reserve. Provider overages are NOT assumed disabled.

Illustrative 31-day forecast: one discovery page/run, nine uncached candidates needing two calls each, six verification calls. Daily = 55; every-other-day = 40; weekly = 29; event-driven enrichment only = 24. These are forecasts, not measured demand. Recommend authorized MLS daily/incremental + event-driven enrichment + optional weekly one-page RentCast discovery. More pages, retries or candidates raise demand; caching lowers it. A request can return only partial RentCast coverage, never MLS-equivalent coverage. Approval is still required before any real requests.

## Coverage and dry run

`scripts/discovery_tools.py --synthetic --output <directory>` produces the committed fictional report and budget forecast without network or email. `--benchmark <manifest> --output <file>` reads the existing database read-only. Manifest requires permission_id, scope, as_of and listings containing canonical entity key, relevant, listed_at, status and ZIP. Benchmark collection must itself be authorized. Measures source/union recall, unique/missed records, within-24h discovery, delay, status comparison, duplicate frequency, missing finance and ZIP gaps. A fixture comparison is not evidence of real-world coverage.

Fictional cohort: MLS 75%, RentCast 50%, union 100%; actual MLS coverage remains unknown. The 95% and 24-hour targets are acceptance goals, not achieved claims. A licensed same-scope reference export is needed to measure them. Listing deletions, relistings and address aliases need actual feed acceptance; conservative merging may leave duplicates requiring review.

## Safe deployment and rollback

1. Review PR #9 authentication changes and this stacked PR; do not merge automatically. Confirm current master and backup database before any authorized release.
2. Existing Render web/cron stay on master. Keep auto-deploy implications in mind: merging to master would deploy. Do not merge until explicit deployment approval. No Blueprint apply or new service is required.
3. Deploy code only with all new flags false and `DISCOVERY_SOURCE` unchanged. Existing startup runs `Store.migrate()`; new tables are additive and idempotent. This still changes the production schema and needs explicit approval first. Back up existing DB. No data rewrite/drop/backfill is required. Do not run a second migration job concurrently with startup.
4. Confirm old email behavior, health, Basic Auth and disabled new endpoints. Existing nightly/daily schedule is unchanged. `ENABLE_UNIFIED_DAILY` must remain false during controlled inbound tests. Set only source-specific permission/configuration after a licensed feed is approved.
5. With explicit permission, ingest one controlled export or forwarded alert, no delivery or paid request. Verify signatures, source identity, idempotency, freshness, taxes and saved observations. Capture one regular RealScout alert sample separately. Validate Trestle metadata, eligible active records, status/deletion behavior and an authorized reference benchmark before claiming connected MLS coverage.
6. Configure approved underwriting inputs and current RentCast cycle baseline securely, keep all RentCast flags off until specific request approval. Prepare a report without sending. Review pending reasons and fictional-data exclusions.
7. Only after explicit cutover approval set `DISCOVERY_SOURCE=unified`, enable unified discovery/daily and approved source profiles. Keep the existing cron; no parallel legacy daily pipeline. Enable paid enrichment only within approved caps. Authorize one real email acceptance test separately.

Rollback: disable unified source flags and restore prior DISCOVERY_SOURCE/approved environment values, then redeploy prior code if needed. Preserve additive tables, original counters, outbox and history; never reset quotas or delete outbox to force a resend. Review an attempted/uncertain delivery before retrying. Failed provider health clears on the next genuine success; reports show current source failures rather than stale operational warnings.

## Configuration (existing Render services only)

Defaults: `ENABLE_UNIFIED_DISCOVERY=false`, `ENABLE_UNIFIED_DAILY=false`, `ENABLE_MLS_EXPORT_WEBHOOK=false`, `ENABLE_UNIFIED_RENTCAST_DISCOVERY=false`, `ENABLE_UNIFIED_RENTCAST_ENRICHMENT=false`, `ENABLE_UNIFIED_STATUS_VERIFICATION=false`, `ENABLE_UNIFIED_RENT_ESTIMATES=false`.

`UNIFIED_PROCESSING_APPROVAL_ID` records permission, not a secret. `UNIFIED_SOURCES_JSON` lists profiles with `source` contract plus `transport` (`trestle`, `reso`, `csv_file`, `csv_webhook`, `realscout`, `rentcast`, `wprdc`). RESO profiles accept base_url/token_url/expected_system/max_pages/bearer_env. CSV accepts secret_env or export_path/manifest_path. Keep source profiles disabled until authorized. Credentials go only in Render secret variables (`TRESTLE_CLIENT_ID`, `TRESTLE_CLIENT_SECRET`, `MLS_BEARER_TOKEN`, dedicated CSV secret reference). Do not put credential values in JSON or GitHub.

`UNIFIED_ASSUMPTIONS_JSON` maps input names to `{value,kind,source,approval_id}`. `UNIFIED_FINANCING_MODE` blank/cash/loan. `UNIFIED_RENTCAST_INTERVAL_DAYS=7`, `UNIFIED_MAX_ENRICHMENT_CANDIDATES=3`, `UNIFIED_MLS_REVERIFY_LIMIT=3`. `RENTCAST_DISCOVERY_CAP=8`, `RENTCAST_ENRICHMENT_CAP=22`, `RENTCAST_VERIFICATION_RESERVE=10`. Existing MAX_API_REQUESTS_PER_RUN/DAY/MONTH, API_APPROVAL_MODE/ID, APPROVED_API_REQUESTS, BILLING_CYCLE_ANCHOR and BILLING_USAGE_BASELINE remain mandatory controls. PUBLIC_APP_URL must be the existing dashboard URL for protected RealScout links. Existing DATABASE_URL, email settings and Basic Auth are reused.

## Resend and Gmail manual connection (not performed)

First confirm agent/RealScout permission for forwarding and third-party processing. In Resend, configure a dedicated inbound subdomain/address using its current Receiving DNS instructions; preserve existing outbound DNS. Create an `email.received` webhook pointing to the existing dashboard's `/webhooks/resend/inbound` endpoint, store its signing secret and receiving API key securely in Render. Configure the precise alias, original sender/domain and original recipient policy, encryption key and raw download host using PR #9 documented variables. Never trust a wildcard raw host or raw Authentication-Results header.

In Gmail Settings → Forwarding and POP/IMAP, add only that dedicated address and complete Gmail's confirmation. Global forwarding stays disabled. Create a filter for the approved exact RealScout sender and saved-search mail, forwarding only those messages. Confirmation mail must not be treated as property data; retrieve the confirmation through the authorized receiving workflow only with explicit approval. Do not process arbitrary mailbox contents. Confirm Gmail preserves the original DKIM signature and signed body in one approved forwarded sample before enabling background ingestion. Signature failure means quarantine/reject, not an allowlist bypass. Receiving availability/limits and the account's charges must be checked before activation; no new subscription has been purchased.

See REALSCOUT_DEPLOYMENT.md for exact webhook path and settings, and MLS_ACCESS_RESEARCH.md for licensed feed options. No secrets, real email content or original sample identifiers are included in this change.
