# Discovery policy — November 11, 2026

The canonical `data/bedroom-price-policy.json` is copied unchanged into PENTEN RE.
Two-bedroom homes have an inclusive $150,000 ceiling. Three/four-bedroom homes
retain the inclusive $200,001 ceiling. All geography, condition, property-type,
size, bath, verified-tax and supported high-rent requirements remain in force.

The existing Render cron runs both 08:00 and 20:00 Asia/Jerusalem slots
(05:00/06:00 and 17:00/18:00 UTC, with the local-time guard). No new service or evening email is created. Both use the
existing database lock. `discovery_run:<date>:<slot>` records outcome and request
counts; completed slots cannot fetch twice. Failed sync deliveries retain the
existing recovery ledger. Source failures and partial/budget-limited scans
remain explicit, including when zero properties qualify.

Expanded RentCast queries request 2–4 bedrooms and at most $200,001, with backend
enforcement of the lower two-bedroom ceiling. Each request supports 500 records.
Query-policy-specific cursors preserve offsets and rotate Pittsburgh/Bellevue,
Pleasant Hills, and Bethel Park after a partition completes. Cursors from the
older three-bedroom query are retained as history and never reused for this
expanded query. Coverage is observed inventory, not a complete-market claim.

The currently approved provider budget remains authoritative. No subscription,
overage, quota, or recurring approval is activated by this change. Insufficient
capacity produces a budget-blocked/partial scan. Authorized RealScout Gmail
intake remains unchanged; two-bedroom saved-search coverage requires actual
authenticated RealScout configuration or alert evidence and is not assumed.
