# Independent Pittsburgh MLS access — decision record, 2026-10-24

Supersedes the earlier agent-sponsored recommendation. No agent/broker-sponsored route is proposed or pursued. Public official sources were reviewed without contacting providers, creating accounts, retrieving private data or making paid calls.

**Decision: BLOCKED PENDING LICENSING.** No independently obtainable West Penn feed is confirmed. The best evidenced lead is a direct, private-use license from West Penn, delivered through its existing Trestle connection if West Penn authorizes it. This is a lead for a licensing decision, not an available subscription or a connected source. Becoming a technology provider alone does not confer data rights. Independent access is not established as technically impossible.

## Short decision table

| Route | Coverage / automation evidence | Independent eligibility and use | Cost / commitment | Decision |
|---|---|---|---|---|
| Direct West Penn private-use license | West Penn is the relevant MLS; public material documents brokerage IDX, not an investor analytics API | Published IDX requires brokerage subscribers. A separate non-member/private-analysis agreement, any exemption from sponsorship, internal storage, automated scoring and owner-only emails all require written confirmation | No applicable private-use setup/monthly/MLS fee, minimum term or refresh SLA published in reviewed material | Ask the actual licensor first; pending, not confirmed available |
| West Penn via Trestle/Cotality | Official public active-source list names West Penn MLS, Inc.; OAuth/OData, replication and incremental query mechanisms documented | Trestle facilitates the MLS license; it does not grant it. FAQ warns that member-serving uses and broker/agent ratification are typical. No West Penn independent-investor exception verified | Small RESO connection $100/month; $105 from January 2027, PLUS unknown MLS fees. Private-use setup, minimum commitment and other charges unverified | Preferred technical delivery only if West Penn expressly approves independent use |
| RentCast (NON-MLS fallback) | Official nationwide sale-listing API, Active/Inactive records, prices, listing dates/history. Advertised daily updates and 12–24h new-listing ingestion; no measured Pittsburgh completeness/SLA | Self-service API terms explicitly permit internal analytics and storage. No MLS membership/sponsor prerequisite documented. It explicitly does not source listings directly from MLS | Developer includes 50 requests/month; existing account entitlement supplied by owner, not privately inspected. Overage automatic; no provider hard cap. No long-term API commitment | Most practical existing permitted fallback; not MLS access and not activated by this work |

### Primary evidence

- [West Penn IDX and official contact](https://www.westpennmls.com/idx/): subscriber/brokerage program; `inbox@westpennmls.com`, 412-367-5860. Public IDX material does not establish eligibility for this separate private use. No inference that absence of a posted program equals a refusal.
- [Trestle public subscription/source catalog](https://trestle.corelogic.com/SubscriptionWizard): search-indexed active-source list explicitly includes West Penn MLS, Inc. The interactive catalog can render only its initial form in some clients. No account was created.
- [Trestle licensing/pricing FAQ](https://trestle-documentation.corelogic.com/support/faq/): MLS is licensor, approval workflow and fee structure. Source availability is distinct from buyer eligibility. Refresh aim is within five minutes; not a contracted West Penn SLA or proof of full inventory/status rights.
- [Trestle technical getting started](https://trestle-documentation.corelogic.com/web-api/), [query reference](https://trestle-documentation.corelogic.com/web-api/reference/) and [Property dictionary](https://trestle-documentation.corelogic.com/metadata/resources/Property/): documented protocol compatibility, not live entitlement.
- [RentCast API license](https://www.rentcast.io/terms-api), [listing provenance/freshness](https://developers.rentcast.io/reference/property-listings), [sale API](https://developers.rentcast.io/reference/sale-listings), [billing](https://developers.rentcast.io/reference/billing-and-pricing): different from its website/platform terms. Provider coverage/freshness statements remain vendor claims until independently benchmarked.

## Vendor elimination — no speculative integrations

[Repliers' West Penn directory](https://repliers.com/mls-directory/pennsylvania/west-penn-multilist/) is credible market evidence, but its [licensing requirements](https://help.repliers.com/en/article/mls-requirements-for-data-licensing-when-signing-up-with-repliers-12fknh6/) require the customer's MLS authorization and describe member-controlled products; paying Repliers does not authorize an investor's independent product. Therefore it is not an available independent route.

[ATTOM Active Listings](https://cloud-help.attomdata.com/article/516-active-listings) explicitly requires licensing with at least one MLS for Slipstream. Pittsburgh entitlement and a direct independent-use license/price were not verified. [Bridge](https://www.zillowgroup.com/developers/api/mls-broker-data/mls-listings/) is invite-only at MLS discretion; relevant independent rights were not verified. [HouseCanary's developer guide](https://www.housecanary.com/blog/real-estate-data-api-for-developers) explicitly distinguishes its Data Explorer API from a live MLS feed. Its national investor marketing is not evidence of a West Penn listing API license. SimplyRETS public API/pricing alone does not establish this market or use authorization. No new vendor integrations were built.

## One inquiry to settle the licensing gate — prepared, NOT sent

To: inbox@westpennmls.com
Cc: trestlesupport@cotality.com
Subject: Independent private-use West Penn data license / Trestle eligibility — RentalSourcing

Hello West Penn Data Licensing and Trestle teams,

I am an independent real estate investor, not an MLS member or brokerage, and am not seeking broker sponsorship. I operate RentalSourcing, a private application that identifies Pittsburgh residential purchase opportunities, stores normalized listings/history in PostgreSQL on Render, calculates investment metrics and sends one daily report only to me. There is no public IDX website, resale, lead marketing or data redistribution. Access may occur from Israel.

Before I create an account or incur fees, please confirm:

1. Can West Penn contract directly with me or my entity for this exact use without MLS membership, a real estate license, sponsoring broker/agent, member customer or brokerage signatory? If yes, what license/product and signatories are required? If no, please state whether any independently licensed distributor can authorize this use and identify it.
2. Can the approved license be delivered through Trestle RESO Web API? Please identify the product, originating-system identifier, metadata endpoint, allowed geographic/property scope and whether it includes all eligible Pittsburgh listings rather than selected broker inventory or syndication opt-ins.
3. Which statuses are licensed (active, coming soon, pending, withdrawn/expired/cancelled/closed), how are price changes, removals, corrections and relistings delivered, and what update latency, coverage exclusions and support commitments apply?
4. Are PostgreSQL replication, cross-provider deduplication/enrichment, derived investment scores, stored price history, backups, Render/cloud hosting and my private daily email expressly permitted? Please provide attribution, audit, security, geographic access, retention and termination/purge requirements, including whether derived history must be deleted.
5. What are all setup, platform, MLS, per-user/connection, usage/overage and recurring fees, taxes, minimum commitments and cancellation terms? Trestle lists a $100 small RESO connection ($105 from January 2027); please confirm the applicable total quote and any different product tier.
6. Can you supply the current agreement and an authorized test sample or sandbox covering ListingKey/ListingId/system, address/unit, status, asking price, beds/baths, area, coordinates, property type, annual tax/year, HOA/period, parcel, modification timestamps and listing links where available? Please confirm query/replication quotas, removal synchronization and any fields restricted under this use.

Please give a written eligibility decision and itemized quote before any account activation or payment. I am not requesting shared credentials or access under another party's license.

Thank you.

## Connection-ready technical review

Existing PR #9 security/queue and PR #10 multi-source storage, budget, reports and connectors were inspected; none was replaced. Trestle's documented OAuth client-credentials form (`grant_type=client_credentials`, `scope=api`) and bearer authentication match the connector. URLs remain `https://api.cotality.com/trestle/oidc/connect/token` and `https://api.cotality.com/trestle/odata/`. Pagination follows `@odata.nextLink`, with same-origin/resource and credential-leak guards; default page size500 is below the documented1000 maximum. `$filter` date/string syntax, ModificationTimestamp ordering, five-minute overlap and durable page checkpoints are exercised offline. Expected system must come from approved metadata, never a guessed marketing name.

**Concrete fix:** a recognized ResidentialLease/CommercialLease record previously raised the same ValueError as a malformed purchase and stopped its whole page. It now raises a typed non-purchase exception; scan reports an explicit skipped count and continues valid sales. Point verification never refreshes a purchase with rental evidence. Wrong-source records and malformed purchases still fail closed without advancing the watermark. No investment thresholds or sale property-type restrictions changed.

A new mocked contract test checks actual documented token form, token renewal, initial/incremental queries, price reduction and transition to Pending. Status/price eligibility filters are deliberately not pushed into the incremental query: doing so would hide properties moving out of eligibility. Licensed geographic scope and initial history size must be agreed before a real bootstrap. Full replication >1million records uses a different documented endpoint; this bounded Property connector intentionally rejects that path rather than pretend to support it. If that scale is required, approve the product/scope and corresponding implementation first. No live metadata/schema compatibility can be certified without access.

## Objective CONNECTED acceptance gate

All items are required; passing fixture tests is insufficient:

- Written independent-use license with all required approvals, fees and permitted processing confirmed.
- Scoped authorized credentials installed securely; actual product metadata/field types and system identifier inspected.
- Approved bounded retrieval returns real Pittsburgh purchase listings; normalized data persists in isolated test schema and identities/units reconcile.
- A second approved retrieval verifies real incremental updates, price changes and status transitions; removal/revocation behavior is confirmed for the contracted product.
- Same-scope authorized benchmark measures recall, unique/missed listings and discovery delay over repeated snapshots. Report denominator, exclusions and observation window; do not reuse national marketing percentages as Pittsburgh coverage.
- Actual account request volume and total quoted/billed costs fit the owner-approved budget, including initial load and retries.
- Only after explicit deployment/migration/activation approval may existing production workflow change.

Current result: **NOT CONNECTED — BLOCKED PENDING LICENSING**. No permission, live credentials, real-listing retrieval, production coverage/freshness or commercial quote has been verified. A positive provider email alone still does not satisfy the connection gate.

## If independent MLS licensing is refused

Keep the MLS connector disabled and the MLS requirement open; request a named distributor with explicit rights only if West Penn identifies one. Reuse the existing RentCast sale-listing adapter, not generic assessor records, as the labeled non-MLS fallback. Its terms permit internal analysis/storage; daily status/price observations use the existing evidence/history and report pipeline. Actual Pittsburgh coverage remains unmeasured and listing history is not assumed to expose every intermediate price edit.

No production or allowance change was made. Existing default discovery allocation8 means a daily fallback cannot simply be enabled. An OPTIONAL owner-approved 31-day budget allocation could be31 one-page discovery calls +6 verification +3 enrichment =40, preserving the existing internal ceiling and ten calls of headroom under50. This requires reallocation (discovery_cap31/enrichment_cap3/verification_reserve6), interval1 and verified cycle baseline; these are proposals, not settings applied or a new spend approval. One page is at most500 results and is not a completeness guarantee. Retries, extra pages or enrichment consume those same limits; stopping may sacrifice freshness. More volume requires budget/access decisions, not overages. With current settings, use only already-approved cadence and label cached/no-scan days accurately. A daily email is not evidence of a daily fresh scan.
