# Stored-evidence verification backlog

No RentCast calls, new tax estimates, changed qualification limits or new paid
services. Existing 168-hour complete-tax and 6-hour negative-cache TTLs remain.

## Zero-cost operations

Startup first recovers valid, identity-matched RentCast property responses left
in response_cache after an interrupted write to tax_history. Source, original
retrieval time and original expiry are preserved; evidence is never refreshed
by copying it. Expired/foreign/demo/conflicting/newer-invalid evidence cannot be
promoted. Existing criteria reevaluation then applies every hard filter.

Pending review order is lexicographic: all non-tax criteria known, fresh listing,
reliable observed price reduction, lower asking price, more beds/baths/area,
then observation time and stable ID. This is a factual review order; no rent,
yield or profitability is estimated and no new acceptance threshold exists.
Tax tasks retain PR20's untouched-before-rechecked/FIFO-age/least-recent-attempt
fairness, with factual ordering inside the same age bucket. Pending email cards
retain PR20's unseen/least-recently-shown rotation, with factual ordering inside
the same visibility bucket. A historical reduction followed by an increase is
not reported as a current observed reduction.

The existing email snapshot remains PENTEN's source. Pending properties stay
pending and receive an official research link/reason; no receiver schema change.
Review status, rejection tombstones, discussions and inspection records are not
modified. All candidates remain in storage even when outside the 50-card report.

Aggregate startup logs show cache recovery, fresh/stale tax backlog and approval
ledger consumption. Top source IDs are SHA-256 hashes in logs, never addresses.
A private complete top-ten report is available in the existing Render Shell:

`python -m rentalsourcing.tax_backlog --report`

This command enforces a PostgreSQL READ ONLY transaction and uses no APIs,
emails, migrations or configuration writes. It prints property facts to the
owner's terminal, not application logs. Source timestamps are retained.

## Official source verification, November 1, 2026

- https://data.wprdc.org/dataset/property-assessments : Allegheny County's
  published parcel data, CC0. Data change daily, publication monthly.
- https://data.wprdc.org/dataset/property-assessments/resource/65855e14-549e-4992-b5be-d629afc676fa : API resource; PARID and physical-address/unit fields
  support matching. COUNTYTOTAL/LOCALTOTAL are assessed values, not tax bills.
  TAXYEAR denotes assessment applicability, not proof of a complete annual bill.
- https://tools.wprdc.org/property-api/ : documented comma-separated parcel batch
  lookup aggregates assessments, sales and related records. Direct documentation
  fetch returned 502 during verification; indexed official documentation was
  readable. No complete annual-tax-bill dataset was established from it.
- https://realestate.alleghenycounty.us/search : supported browser search by
  address/parcel. No automated bill endpoint/permission or rate limit has been
  established. No scraping or undocumented endpoint was introduced.
- https://alleghenycountytreasurer.us/real-estate-tax/ : county billing plus
  separate local/school millage resources and relief programs. A county-only bill
  does not establish the total annual property tax required by RentalSourcing.

Published assessments, delinquent balances, liens, tax rates and derived millage
calculations cannot replace latest reliable annual tax evidence. A supported
bulk full-bill source covering county/municipal/school taxes remains unconfirmed.
No alternative-source properties have been marked tax verified. Research links
are not verification. Actual official bills would require reliable parcel/address
match, applicable year, source document, retrieval date and validated scope before
qualification; no speculative importer was activated without such evidence.

## Release safety

No schema migration, budget, schedule, credentials or synchronization contract
change. Deploy same reviewed merge to dashboard then cron. Existing startup
lock protects cache recovery and reevaluation. Roll back code if needed while
retaining recovered evidence/history. Do not reset counters or resend outbox.
