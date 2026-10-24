# Listing discovery MVP — current product policy

The current owner request supersedes the earlier default dual-underwriting workflow. The daily unified report defaults to `UNIFIED_REPORT_MODE=mvp` and `ENABLE_DUAL_UNDERWRITING=false`. No financial assumption approvals are needed for MVP listing eligibility. No providers, schedules or delivery settings are activated by these defaults.

## Eligibility

`discovery.evaluate.screen` uses the unchanged maintained `core.verification` and `core.tax_verification`: Pittsburgh municipal polygon, PA, Active for-sale, asking price up to $200,000, at least 3 bedrooms / 1 bathroom / 1,000 sqft, ZIP exclusions 15203/15204/15210, and latest verified annual tax plus $100 strictly below $2,000. Known hard failures reject; missing or conflicting required evidence stays Pending Verification. Existing normalization, freshness windows and source resolution are retained. There is no additional profitability or property-type threshold.

Tax amount/year remain an atomic latest-year pair. A newer reported tax year without an amount cannot borrow an older amount. Estimates are not verified bills. Unknown HOA, rents, insurance, renovation, loan assumptions or conflicts in financial-only values do not block MVP qualification. Any ambiguity about the original hard filters does block. Qualification means listing criteria verified, not an investment recommendation.

## Report and persistence

New qualifying listings, qualifying reductions, newly verified listings and recently observed pending candidates have separate sections. Old unchanged qualified inventory is retained in PostgreSQL but omitted from daily candidate sections. Reductions must match the currently resolved price; there is no invented minimum reduction percentage. Repeated cross-source alerts use existing canonical IDs and do not create duplicate candidates. Source events retain their original discovery dates.

The listing cards contain address, asking price, beds/baths/area, ZIP, latest verified tax/year, status, discovery sources, first discovery timestamp, direct source links and unresolved hard evidence. No underwriting, score, loan or expense scenarios are rendered. Mobile HTML cards and matching plain text use escaped content. Source outcomes distinguish fresh/partial discovery, no new alerts, cached data, skipped scans, source failures and internal API budget exhaustion. Absence of qualifying properties is a separate statement from scan success.

No schema migration is required. Reports store `report_mode=mvp` in existing `discovery_reports`. Historical snapshots without this key use the historical renderer and validation. Once attempted, the existing outbox body remains frozen. New MVP validation rechecks hard evidence, source version, tax and freshness without requiring financial calculations. Changes fail validation rather than rewriting an attempted email. Existing daily outbox key and uncertain-delivery recovery are unchanged. Synthetic reports remain unsendable.

## Optional future module

All dual calculations, ranking, sensitivities, tests and historical synthetic examples remain. Future explicit activation requires BOTH `UNIFIED_REPORT_MODE=advanced` and `ENABLE_DUAL_UNDERWRITING=true`, with separately approved and provenance-labeled inputs. A stale true dual flag alone cannot enable advanced reporting while mode is MVP. `legacy` mode exists for historical regression/compatibility, not the MVP activation path. Earlier dual-underwriting and finance-decision documents describe the optional module, not MVP prerequisites.

## Controlled activation after explicit production approval

1. Follow `PRODUCTION_ACTIVATION_RUNBOOK.md`: backup/outbox review, control existing auto-deploy, one reviewed combined release containing PR #9 and #10, dormant dashboard then matching dormant cron. Keep provider flags off and existing schedule/delivery unchanged. Do not apply Blueprint defaults over live settings.
2. On the existing services configure `UNIFIED_REPORT_MODE=mvp`, `ENABLE_DUAL_UNDERWRITING=false`; leave financial JSON maps empty unless preserving historical configuration. Keep RealScout/MLS/RentCast and enrichment flags disabled pending separate access/cost approvals. No new service or subscription is needed.
3. Configure one authorized source only after its own permission and credentials are verified. MLS remains BLOCKED PENDING LICENSING; correspondence is already in progress. RealScout remains disabled until processing permission, sender authentication and regular-template acceptance are satisfied. RentCast fallback needs the actual billing anchor/account baseline, explicit request approval and internal purpose budgets within its 50-request plan. It is not MLS-equivalent coverage.
4. Authorize a bounded ingestion and no-send preview. Verify actual Pittsburgh data, fresh source timestamps, verified tax/year, links, reductions, deduplication, source failures and budget ledger. Missing tax/coordinates must remain pending, even if this yields no qualifying listings. Fixture tests cannot establish live coverage or availability.
5. Only then authorize the existing cron's unified daily cutover and one real report acceptance. Preserve its schedule, daily idempotency key, outbox, historical data and request counters. If rollback is needed resolve pending unified drafts before an old cron can retry them; do not delete new tables or reset usage.

Remaining barriers: authorized live discovery access (licensed MLS or separately approved fallback), actual source field completeness/tax verification, approved paid-request budget where used, live source/coverage acceptance, production release/configuration and real-delivery authorization. Financial underwriting assumptions are NOT an MVP blocker.

## Offline demonstration

`python scripts/mvp_dry_run.py --output docs/examples/mvp-synthetic` produces HTML/plain text/JSON using only fictional data, blocked HTTP, in-memory SQLite and no email. Three properties demonstrate a new match, a reduction from $205,000 to $195,000 now matching, and a missing-tax pending case. All values/links are fictional; this is not a real Pittsburgh coverage measurement.

## Executed validation

- Full isolated PostgreSQL 17 + SQLite regression suite: **703 passed, 10 skipped**, 56.91 seconds. Skips: seven SQLite variants of PostgreSQL concurrency checks and three opt-in private-message fixture checks. No paid/live provider transport or email was used.
- Dedicated MVP suite on both databases: **52 passed**, 3.93 seconds. Includes hard-filter boundaries, latest tax pairing, missing/estimated tax, financial-only conflicts, source states, deduplication, price reductions, rendering/escaping, historical behavior, frozen outbox and synthetic fixtures.
- Existing dual underwriting suite remains in the full regression suite; advanced behavior tests explicitly opt in. Legacy simulation fixtures explicitly retain legacy mode.
- Compileall, offline build preflight and whitespace checks passed. Changed files scanned for private-key/API-token patterns and sensitive configuration/original `.eml` files: no findings. Core hard filters, polygon, connector implementations, schema and production Blueprint were not changed.
