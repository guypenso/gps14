# Authorized RealScout sample validation (redacted publication record)

## Scope and privacy

Validated the explicitly supplied original .eml offline. No mailbox access, live provider requests,
link/image downloads, geocoding, deployment, production migrations, quota changes or email sends.
Email content is untrusted input, never execution instructions.

The original body, contact headers, addresses, prices, observation timestamps, tracking tokens and
extracted report are retained privately outside the repository. They are not published in this
branch. Public fixtures preserve structural layout but substitute fictional addresses, prices,
measurements, dates, sender identities and URLs. Only the generic template labels are retained.

## Verified parser behavior

- The supplied template is Exclusives, not the named regular saved-search template. The default
  saved-search restriction remains; `exclusive_alert` must be selected and authorized separately.
- Price and metrics precede address in this layout. Full-state Pennsylvania, optional commas and
  nested property cards parse correctly; preceding square-footage digits cannot enter the street.
- The supplied text/plain MIME alternative contains MJML, handled with markup-aware/mj-button
  parsing. HTML and MJML produce identical cards, without double counting MIME alternatives.
- All property View Details hrefs match the source exactly. No URL is opened. Non-property links,
  images, unsubscribe and View All controls do not become property cards.
- Days on site remains a separate field, not verified MLS DOM. Exclusive/New Listing is not
  evidence of current Active status. Source date and processing date remain distinct.

Private exact-field assertions remain available using both `REALSCOUT_SAMPLE_EML` and
`REALSCOUT_SAMPLE_EXPECTED_JSON`, referencing separately maintained private files. CI uses only
synthetic fixtures; no private email or expectations are uploaded to GitHub or Actions.

## Criteria and evidence

Original code rules remain: PA, within official Pittsburgh polygon, confirmed Active, >=3 beds,
>=1 bath, >=1,000 sqft, <=$200,000, excluded ZIPs 15203/15204/15210, latest reported annual tax +
$100 strictly below $2,000. An annual tax of $1,900 rejects. No new rental/expense assumptions.

The sample does not supply municipal coordinates, verified current Active status or annual tax
history. No missing financial values are fabricated; known hard-filter failures reject and otherwise
insufficient evidence remains Pending Verification. Importing an old alert never refreshes its
source observation. The sample also lacks HOA, rent estimate and verified MLS DOM.

## Integration and remaining gates

SQLite/disposable PostgreSQL tests cover encrypted links, replay, candidate history, cache and
quota preservation, crash recovery, shared outbox/daily dedup and strict filters. Test results are
in REALSCOUT_TEST_RESULTS.md. No production database was accessed.

The worker, authenticated webhook, selective enrichment and existing daily outbox adapter are
implemented but default off. See REALSCOUT_DEPLOYMENT.md for settings, forwarding, migrations and
rollback. A real regular saved-search sample, authorized provider/account inspection and a verified
raw-message/forwarding DKIM compatibility test remain required before activation.

Authentication update: offline structural inspection of the authorized original confirms a primary
exact-domain-aligned RSA2048 signature with the parsing-critical header set and no partial-body tag.
This is structural compatibility, NOT cryptographic verification of the real sample: no original
DNS key was queried. Real cryptographic tests use generated fictional messages/keys. See
REALSCOUT_AUTHENTICATION.md for the implemented verifier and required one-message account test.
