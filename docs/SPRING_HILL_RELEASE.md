# Spring Hill eligibility and targeted recovery

ZIP 15212 requires coordinates inside the bundled official Brighton Heights OR
Spring Hill-City View polygon. Labels alone never qualify. Bellevue, Sheraden,
excluded ZIPs, townhouse and all numeric/tax rules remain unchanged. Discovery's
existing circle covers both neighborhoods: no extra request or schedule change.
The manifest records the official query, retrieval time and SHA-256; missing or
changed files fail closed. No migration. Deploy dashboard then cron at the same
merge; existing startup reevaluates eligibility without deleting history.
Rollback code to the preceding merge if necessary; retain all data.

In the updated Render dashboard **Shell**, run:

```sh
python -m rentalsourcing.targeted_sync --execute
```

If cron-only credentials are absent, hidden prompts request the existing signing
secret and separate Sites service token. Read these privately from
`rentalsourcing-daily` → Environment. Never paste them into chat or change env
settings. A supported cron shell with existing keys needs no prompts.

The runner verifies the original October 28 **sent** outbox and exact selected
Rockledge card, then current source identity, coordinates, hard filters and
cached tax evidence. A rejection stops. Missing tax/stale listing stays Pending
Verification. Original price history determines first discovery. No scans,
emails, migrations, history rewrites, fake outbox rows or API ledger changes.

A dedicated deterministic report ID represents this one-property recovery, not
the entire October 28 report. Bytes freeze in the existing settings table before
transport; retries are idempotent. Changed source evidence fails closed. The
unchanged receiver preserves classifications and permanent rejection tombstones.

Success: `TARGET_SYNC_ACCEPTED count=1 ...` or `TARGET_ALREADY_ACCEPTED ...`.
Then verify the exact property and tax status in the live PENTEN Rental Sourcing
view. Respect any existing saved/rejected classification. A receipt alone does
not prove UI visibility.

The Render connector cannot execute Shell and cannot query this database while
its external allowlist is empty. Do not change network policy to bypass this.
