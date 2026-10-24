# Inbound authentication: verified original DKIM

## Current documentation correction (October 24, 2026 Israel)

The official [Retrieve Received Email documentation](https://resend.com/docs/api-reference/emails/retrieve-received-email) explicitly documents a nullable top-level `authentication` object: SPF, DKIM and DMARC results computed by the receiving server, separate from sender-controlled headers. It also documents raw-message download URLs. This direct documentation supersedes the uncertainty inferred from the older OpenAPI/SDK snapshot below. No private account request was made.

The fixed HTTPS ReceivingClient binds the returned email ID and constructs a typed internal provider-evidence object only from valid API result enums. Sender headers and webhook JSON cannot construct this evidence. Explicit receiver DKIM/DMARC failure vetoes acceptance; processing failures defer. Forwarded SPF failure alone does not override valid original DKIM. Missing/null/gray results never grant trust. Independent full-message aligned DKIM remains required, so this does not weaken PR #9. An approved real forwarding test is still required to validate account availability, byte preservation, sender signature coverage and DNS access. Authentication-Results headers and received_for remain untrusted.

## Historical OpenAPI/SDK findings (October 23, 2026 Israel)

Official Resend OpenAPI `resend/resend-openapi`, main, `resend.yaml` (blob
b8abc922c01f395b502f8d9f49ef3b40a546aee6, API info version1.5.1):
`GetReceivedEmailResponse` exposes generic `headers`, `from`, `to`, message_id and bodies. Neither
this response nor EmailReceivedEventData specifies a dedicated SPF/DKIM/DMARC verdict or a trusted
Authentication-Results namespace/sanitization guarantee. `received_for` is explicitly derived
from the `for` clauses of Received headers; it is NOT authenticated envelope-recipient authority.
Absence of such a contract is not proof that Resend never evaluates mail internally.

Official Resend Node SDK, canary:
`src/emails/receiving/interfaces/get-receiving-email.interface.ts` (blob
f700b06cefda05c60b3ba196bfe986441599eee8) defines OPTIONAL/NULLABLE
`raw: {download_url, expires_at}`. `receiving.ts` and `receiving.spec.ts` retrieve this original
message to implement forwarding. The current OpenAPI response omits raw: this is a published
schema discrepancy, not a guarantee that every account/message has the field. A controlled account
test must establish availability, exact download host, unchanged bytes and outgoing DNS access.

Direct resend.com documentation access from this investigation environment was blocked by its
CONNECT proxy403. The sources above are Resend's official public API definition/SDK, fetched through
authorized read-only GitHub access. No private Receiving API/mailbox/DNS lookup was performed.

References:
- https://github.com/resend/resend-openapi/blob/main/resend.yaml
- https://github.com/resend/resend-node/blob/canary/src/emails/receiving/interfaces/get-receiving-email.interface.ts
- https://github.com/resend/resend-node/blob/canary/src/emails/receiving/receiving.ts
- https://github.com/resend/resend-node/blob/canary/src/emails/receiving/receiving.spec.ts
- https://resend.com/docs/api-reference/emails/retrieve-received-email
- https://resend.com/docs/webhooks/verify-webhooks-requests
- https://developers.cloudflare.com/1.1.1.1/encryption/dns-over-https/make-api-requests/dns-wireformat/

## Trust boundary and implemented alternative

No manual header-trust waiver can enable authenticity. Legacy REALSCOUT_TRUSTED_AUTHSERV_ID and
REALSCOUT_AUTH_CONTRACT_APPROVAL_ID must be empty; nonempty values fail configuration validation.
The retired trusted_authentication helper always returns no evidence. Raw Authentication-Results,
Received, ARC headers, untyped authentication dictionaries and From allowlisting never grant trust. Provider-generated results are handled only through the typed transport boundary described above.

Instead, the EXISTING app verifies original DKIM itself:

1. Svix raw-byte/timestamp signature authenticates Resend's notification. Exact dedicated alias,
   sender/profile and durable email/event IDs gate the queue. `received_for` is never authority.
2. Existing fixed HTTPS Receiving API GET retrieves only the queued UUID. TLS/key authenticate the
   provider metadata and bind returned ID; no mailbox/list enumeration.
3. RawDkimVerifier downloads only that response's unexpired raw URL. HTTPS, an exact privately
   approved storage-host allowlist, no userinfo/IP/non443 port/fragment, no redirects, no Resend
   Authorization/cookies on the download, 20-second socket timeout and 2MB limit. URLs/tokens and
   raw content are not logged or persisted. Provider HTML/text is not parsed as listings.
4. DKIM uses pinned dkimpy with an injected bounded DNS-over-HTTPS lookup to a FIXED Cloudflare
   endpoint (standard DNS wire format over verified TLS). No account/key/subscription needed.
   Query/response binding, DNS error/alias limits and TTL-bound PostgreSQL cache are enforced.
   Resolver DNSSEC checking is not disabled; unsigned DNS zones remain conventional DKIM trust,
   not a claim that every key has end-to-end DNSSEC. Raw public keys are cached, not private keys.
5. Require RSA-SHA256 with >=2048-bit key and EXACT `d=` alignment to the allowed original From
   domain. No relaxed/suffix-domain guess and no SHA1/partial-body `l=` acceptance. At most five
   signatures, bounded DNS work; missing/unverifiable evidence quarantines or bounded-retries.
6. Singleton From, To, Date, Subject, Message-ID, Content-Type and MIME-Version must all be signed.
   Present root content-transfer-encoding/disposition must also be signed. Exactly one From mailbox;
   signed To must include an explicitly configured authorized original recipient. API message_id/from
   must match the raw message. Missing/duplicate/unsigned parsing-critical headers reject.
7. Signed Date and signature t must be within a fixed seven-day authentication window; future
   skew bounded five minutes, declared x expiry honored. This is an AUTHENTICATION/replay limit,
   not a change to investment/source freshness: listing evidence still uses the existing24h rules.
   Same signed message re-delivered under a new provider ID does not refresh observation timestamps
   or create new cards/reports; existing message-id/content-collision checks remain. Expired old
   messages quarantine for review rather than being called new listings.
8. Parse ONLY the verified original raw MIME. Persist hash/domain/selector/method/message identity
   audit metadata in realscout_auth_evidence, not raw bytes, headers, addresses or signed URLs.
   Verify the queue lease again after download/crypto before committing. Existing retries/outbox/
   quotas/history remain. No RentCast or sending operation is part of verification.

This proves domain-authorized signed content and intended original recipient. It does not compute
SMTP SPF or claim an official Resend DMARC disposition. Exact aligned DKIM supplies the sender-
authentication property needed without trusting forwardable textual verdicts. A compromised sender
account/domain/DNS authority remains outside the protection of ordinary DKIM. ARC-only forwarding,
unsigned/weak messages, altered body/signature, old messages or absent raw bytes do NOT fall back.

## Configuration and deployment

Keep ALL existing RealScout flags off until deployment and controlled receipt are separately approved.
No new web/worker/cron service or paid subscription. Same existing dashboard/webhook and PG database.
Default-off deployment still uses the existing approved migration/startup procedure.

New setting: REALSCOUT_RAW_DOWNLOAD_HOSTS, JSON array of exact lowercase storage hostnames, no
wildcard or inferred broad amazonaws.com suffix. Determine it privately from one approved API raw
response, never from email text. No signed URL or token belongs in env/chat/Git. Keep PUBLIC_APP_URL
as the actual existing dashboard URL. Receiving key and link vault stay private and unchanged.

Each policy adds `original_recipients`:

```json
[{"inbox":"exclusive@YOUR-MANAGED-SUBDOMAIN.resend.app",
  "allowed_senders":["EXACT-ORIGINAL-REALSCOUT-FROM"],
  "original_recipients":["AUTHORIZED-ORIGINAL-GMAIL-RECIPIENT"],
  "profile":"exclusive_alert"}]
```

These are placeholders; provision actual values privately. Worker or daily+ingest activation fails
without storage-host/original-recipient policies. Queue-only intake can stay enabled with worker and
daily OFF during an expressly approved test. All paid enrichment/status flags stay OFF.

Additive RealScout migration version2 creates realscout_dkim_keys and realscout_auth_evidence;
original table/history/cache/quota/outbox data remains. Existing Store.migrate/start_web.sh apply it
only at an approved deployment/runtime invocation. Transfer utility preserves both new tables.

## Smallest controlled validation; NOT performed

Approve receipt/retrieval/verification of ONE authorized automatic Gmail-forwarded Exclusives alert,
with no RentCast request or report send. Use a dedicated receiving alias already available in the
existing account. No general mailbox access or new subscription is needed.

- Owner confirms permitted forwarding and the alias; Gmail forwarding confirmation is completed
  through Resend Receiving. The application never allowlists Google verification messages.
- Receive one approved automatic forward (manual wrapped Fwd/attached .eml is unsupported). Inspect
  the signed notification's alias and Receiving metadata UUID/message_id privately.
- Read ONE metadata response; confirm raw.download_url/expires_at, privately approve its exact
  storage host. Download the one original and verify aligned DKIM using public DNS. Do not copy
  original body, credentials, URLs or keys into the repository/logs/chat. Seven-day replay window
  requires a current alert, not an expired archived one.
- With separate dormant deployment/processing approval and all daily/enrichment flags OFF, execute
  one bounded worker pass (MAX_JOBS=1), then replay its same signed notification to prove one queued
  ID/card observation. Do not start an ongoing background worker just to validate one message.

If raw is null/missing, Receiving access is denied, the download host/format differs, signed recipient
or required headers are missing, Gmail changes signed bytes, the original key is unavailable/rotated,
or Render cannot reach HTTPS DNS: keep the job quarantined/retrying and STOP activation. Metadata-
only routing or a secret alias cannot replace sender authentication. Provider raw availability and
real forwarding preservation are the only provider-dependent compatibility facts not proved offline;
SDK/OpenAPI discrepancy makes this test necessary. Existing paid-call limits are untouched.

No production deployment is needed for the initial metadata/raw compatibility inspection: it can run
in an expressly authorized test environment. This investigation environment currently blocks direct
resend.com documentation access; a live compatibility test also requires approved egress for
api.resend.com, the exact returned storage host, and cloudflare-dns.com, plus Receiving credentials
provisioned securely outside chat. No such network/account change was made here. After a successful
provider test, a separately approved dormant deployment and one worker pass can validate app wiring.
