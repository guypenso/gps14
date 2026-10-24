# Authorized Pittsburgh MLS access — official-source review, 2026-10-24

No MLS credentials, signed license or authorized benchmark dataset is available in this environment. No real listing feed has been connected. A client RealScout account does not establish data-feed rights. Research below uses public official documentation, not private account operations.

| Priority | Route and verified evidence | Required permission / cost / limitation |
|---|---|---|
| 1 | Broker-sponsored recurring RESO-shaped CSV, if Sean's brokerage can provide it | Ask broker/West Penn whether private analysis, third-party hosting/storage and investment emails are permitted. No export capability, fee or permission has been confirmed. Smallest integration is one approved export plus written scope; recurring automation depends on a supported producer, not portal scraping. |
| 2 | West Penn through Trestle | [Trestle's source selector](https://trestle.corelogic.com/SubscriptionWizard) explicitly lists West Penn MLS, Inc. [Technical documentation](https://trestle-documentation.corelogic.com/web-api/) specifies OAuth client credentials, scope `api`, token `https://api.cotality.com/trestle/oidc/connect/token`, OData `https://api.cotality.com/trestle/odata/`. Connector implemented and fixture-tested. Broker/MLS approval and actual connection metadata are still required. |
| 3 | Direct West Penn-approved feed/vendor arrangement | [Official IDX page](https://www.westpennmls.com/idx/) describes brokerage subscriber authorization and written consent; broker may contact the data center at 412-367-5860. Published public material does not establish an individual investor's eligibility, a direct API endpoint, current private-analysis license fee or refresh SLA. |
| 4 | RealScout forwarding, complementary discovery | Existing PR #9 preserved; written processing permission and one controlled authentication test remain necessary. Email alert coverage is not complete-MLS coverage. Regular template still requires an authorized sample. |
| 5 | Other vendors only after explicit coverage/license confirmation | [Bridge](https://www.zillowgroup.com/developers/api/mls-broker-data/mls-listings/) describes MLS-discretion invitation access; West Penn availability and cost remain unverified. [SimplyRETS](https://simplyrets.com/) publishes $49/$99/$199 monthly tiers and $99 connection fee, but West Penn feed support was not verified. [IDX Broker](https://www.idxbroker.com/mls/west-penn-mls-wpmls) advertises West Penn website IDX support; that is not permission for bulk underwriting ingestion. |

[Trestle FAQ](https://trestle-documentation.corelogic.com/support/faq/) lists small RESO connections at $100/month, increasing to $105 in January 2027, plus separate MLS licensing fees. Confirm an actual quote and connection rights before purchase. Its refresh aim is within five minutes, not a contractual coverage or freshness guarantee. Available records depend on the licensed product and permissions. ListingKey is the source key; ListingId alone is not globally unique. Do not guess the actual OriginatingSystemName from marketing names.

The [public West Penn IDX agreement](https://www.westpennmls.com/wp-content/uploads/IDX-AGREEMENT-09-20-2018.pdf) is dated 2018. It is evidence of contractual restrictions, not confirmation of current terms or authorization for this workflow. Request the current agreement covering the precise use, hosting, retention and recipients. [RESO](https://www.reso.org/reso-web-api/) supplies standards, not access credentials or MLS data.

## Message to Sean — prepared, not sent

Sean, can your broker authorize a structured West Penn listing feed for my private RentalSourcing investment analysis, hosted on Render with PostgreSQL and a daily report only to me? Preferred is an existing broker-approved RESO/Trestle connection; otherwise a supported recurring CSV export is sufficient. Please confirm West Penn/RealScout permission for this third-party processing, retention and email use, any vendor sponsorship/contracts and all fees before activation. We need the authorized Pittsburgh residential-for-sale scope, active/status changes and price changes, listing key/system, full address/unit, price, beds/baths, area, coordinates, tax amount/year, HOA/period, property type, parcel ID, modification timestamp and listing link where available. Please confirm refresh frequency and removal rules. No password or shared portal login is needed; scoped credentials can be provisioned securely after approval. Can your brokerage provide this or introduce its MLS data contact?

## Acceptance evidence required

1. Written product/scope/use authorization and fee approval.
2. Scoped API credentials or one authorized structured export; metadata confirms fields and originating system.
3. Controlled ingestion into a test schema, duplicates/status/deletions and missing fields reviewed.
4. Authorized same-scope listing snapshot benchmark, measured recall and discovery delay over repeated days.
5. Only then activate existing daily pipeline after explicit production approval.

No coverage claim can substitute for these steps. If the only licensed route costs more than the approved budget, MLS remains access-blocked; RentCast is complementary, not a permanent MLS replacement.

## Other provider evidence

[WPRDC assessment dataset](https://data.wprdc.org/en/dataset/property-assessments) lists CC0 with portal terms and the [field dictionary](https://data.wprdc.org/en/dataset/property-assessments/resource/65855e14-549e-4992-b5be-d629afc676fa). Assessments and historical sales are not annual tax bills or active listings. Adapter performs exact parcel matching only; no real parcel retrieval was performed.

[RentCast billing documentation](https://developers.rentcast.io/reference/billing-and-pricing) states the 50-request Developer allowance and automatically billed overages; it does not provide a reliable hard spending stop. The application enforces its own limits. [Rent estimates](https://developers.rentcast.io/reference/rent-estimate-long-term) are estimates with comparables, and multi-family values can be per-unit. Automatic whole-property underwriting therefore restricts unsupported multi-family rent interpretation rather than multiplying an invented unit count.
