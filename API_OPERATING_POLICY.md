# Recurring production API policy

The legacy production allowance remains active until provider-account usage and
subscription are verified and a budget is explicitly activated. Deploying this
code does not purchase a subscription or grant new requests.

To activate recurring included-quota usage, configure both Render services:

```
API_APPROVAL_MODE=monthly_budget
MONTHLY_API_REQUEST_BUDGET=50
PROVIDER_INCLUDED_REQUESTS=50
MAX_API_REQUESTS_PER_MONTH=50
BILLING_CYCLE_ANCHOR=<verified provider cycle start with timezone>
BILLING_USAGE_BASELINE=<actual already-consumed account requests>
MAX_API_REQUESTS_PER_DAY=2
MAX_API_REQUESTS_PER_RUN=2
LISTING_REQUESTS_PER_DAY=1
MAX_LISTING_PAGES=2
```

The ceiling is the minimum of the monthly budget, included provider quota, and
application monthly limit. Old approval IDs and lifetime caps are ignored only
in this explicitly activated mode. No overage path is implemented. Atomic
reservations precede every attempt, including retries. The existing cycle key
is retained across activation so switching policies cannot reset usage.
Other applications sharing the account must be reflected in the verified
baseline; the app ledger cannot read provider billing usage automatically.

For an approved Foundation subscription, set included quota 1000 and a monthly
application ceiling such as 900, leaving 100 requests unallocated. Daily/run
limits and reserved listing requests remain separately configurable. Paid tax
lookups remain disabled unless separately authorized.

The daily collector uses the existing broad Pittsburgh/Bellevue bounding
search, exact backend geographic policy, 3:4 bedrooms, bathroom, size and price
filters and up to 500 records per page. Recurring mode honors configured page
and run budgets instead of the legacy hard one-page limit. Incomplete scans
keep their durable offset and report partial coverage; never reset an offset
when continuing a scan. A new query strategy requires a new scan identity.

Price recovery is independent of API usage and email delivery. A separate
advisory lock serializes bounded consumers, a frozen batch survives crashes,
and only durable per-observation receipts advance checkpoints. Invalid receipt
items remain held with reasons. The worker checks every 15 seconds, at most 30
ten-item batches or 25 seconds per pass. Normal continuation remains Partial;
nonretryable failures, held work, a queue above 1000, or stalled synchronization
still raise internal incidents. User-facing notifications remain limited to
price changes and incoming property email.
