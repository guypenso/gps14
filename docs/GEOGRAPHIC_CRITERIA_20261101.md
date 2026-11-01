# Approved listing criteria update — 2026-11-01

Eligibility is unchanged except: verified Pittsburgh ZIP 15212 points must be in
Brighton Heights; Bellevue Borough is an authorized municipal exception regardless
of mailing ZIP; confirmed townhouses/townhomes/rowhouses and structured attached
single-family equivalents are excluded. Architectural-style descriptions alone
never reject an otherwise detached single-family property. Unknown or ambiguous
property types remain Pending Verification. Property-record type evidence from
existing selective tax lookups/cache is also checked; no extra requests are added.

All prior limits remain: active PA residential sale, <=$200,000, >=3 beds, >=1 bath,
>=1,000 sqft, excluded ZIPs 15203/15204/15210/15211, latest annual tax + $100 strictly
<$2,000. Postal city/ZIP never verifies municipal/neighborhood membership. Bellevue
is tested first in geography; excluded ZIPs still override that exception. Unknown
coordinates/borders cannot appear as geographically confirmed opportunities.

Official bundled city/county polygons and hash manifests are described in data/README.md.
A single RentCast circle request replaces mailing-city discovery and includes all
of Bellevue (including 15202) and Pittsburgh. Extra municipalities in the request's
transport envelope are rejected locally. Existing run/day/cycle limits, one-page
daily behavior, no-retry policy, cache and tax budgets are unchanged. A full first
page is still reported as partial coverage; no unmeasured coverage claim is made.

The shared verifier controls ingestion, stored re-evaluation, new/reduction items,
pending email cards and finalized sync snapshots. Frozen unsent selections that
no longer satisfy the rules are cancelled, never silently rewritten; attempted
messages require existing reconciliation. Sent email bodies, report payloads and
historical delivery IDs are preserved. Existing already-sent sync retries retain
their exact historical selection. No receiving contract changes are introduced.

Deployment: merge reviewed PR; deploy existing dashboard first, then cron at the
same merge SHA. Build --check validates all bundled geometry offline. Existing
startup migration is idempotent; no new schema migration exists. Startup re-evaluates
property eligibility and preserves original records, histories, caches, counters
and outbox. Do not trigger a cron run, provider scan or email. Check build/start
logs, health, exact SHA and unchanged cron command/schedule/Auto-Deploy settings.
The next normal daily report uses the updated selection.

PENTEN RE saved review classifications, inspection workspaces, files and discussions
are not modified by this release. Historical imported deals remain historical;
new snapshots mirror only updated email selections. Ineligible RentalSourcing
records retain their IDs/history with Rejected status and explicit reasons.

Rollback: redeploy the previous application SHA; database/history remain intact.
That also restores the former selection policy, so use only for a release failure,
not as a way to send an otherwise disallowed property. No data restore/reset or
counter/outbox alteration is part of this release.
