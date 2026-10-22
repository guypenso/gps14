# Feature-branch publication review — October 23, 2026 (Israel)

Repository: guypenso/gps14. Maintained default branch: master.

## Privacy and credential review

The unpublished implementation commit was rewritten before its first remote push. Real-email-derived
reports, hardcoded real-property expectations and exact message metadata were removed from the
publishable history, rather than left in an ancestor commit. Private validation artifacts remain
outside the repository. The reuploaded authorized sample is byte-identical to the previously tested
sample. It remains Exclusives; it does not validate the regular saved-search template.

Committed fixture messages use fictional addresses, sender/recipient identities, prices, measurements,
dates and fake links. Exact original tests load two explicitly supplied PRIVATE files through
REALSCOUT_SAMPLE_EML and REALSCOUT_SAMPLE_EXPECTED_JSON. CI does not receive those files.
.gitignore excludes original .eml files except the designated synthetic fixture directory, private
validation data and real-sample report outputs. .env.example contains placeholders/defaults only;
no private .env, key/certificate, credential file, DB dump or uploaded message is tracked.

Offline detect-secrets 1.5.0 (`scan --no-verify`, NO network verification) found nine candidates.
Manual review classified all nine as explicit local-test passwords, synthetic signing secrets,
example DB URLs, fixture error text or the public boundary SHA256. No actual secret was found.
A separate tracked-file scan compared original addresses, detail URLs, message ID, contact email
headers and available runtime credential values without printing them: zero matches.

## Verification

Final full suite on Python3.12 and disposable PostgreSQL16 (matching existing GitHub CI):
**412 passed, 7 skipped in 29.82s**. Seven skips are SQLite-only variants requiring PG-specific
cross-session/locking behavior; their PG variants ran. All three private-original sample tests ran.
All RentCast/Resend receiving/sending transports are blocked or mocked by tests.

Existing GitHub CI uses only disposable PostgreSQL, fake test DB credentials, provider flags off,
requirements-dev installation and pytest. Workflow has no deployment, remote database connection,
production migration, schedule mutation, secret upload or mail send step. The PR uses this existing
workflow; it does not add a second schedule/service or modify production configuration.

Offline application --check, compileall, pip check, synthetic dry-run report, YAML/cron comparison
and git diff --check were also verified. The existing Render preview setting is off. The sole Render
cron and its command/expression remain identical. New flags default off, discovery remains rentcast,
paid-call authority/counters are unchanged. Do not merge/deploy/activate based solely on CI success.

Remote CI results and exact PR/commit links are reported in the handover after publication; this
file records the local review, not a claim that remote CI or actual provider forwarding is verified.

## Remaining controlled-test gates

Explicit permitted-use/one-message receipt authorization, secure existing Render/Resend access,
verified receiving-authentication contract and account eligibility. Regular-template enablement
still requires its own .eml. Live sender/recipient delivery status remains unverified. No production
change, real message send, mailbox access or paid property request is authorized by publishing a PR.
