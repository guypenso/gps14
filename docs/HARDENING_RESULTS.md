# Production-readiness hardening — executed 2026-10-24

No production access/change, merge, deployment, vendor contact, private mailbox access, paid API request or real email was performed. West Penn/Trestle licensing responses remain external. PR #10 contains PR #9's security/ingestion ancestry and remains the maintained development branch.

## Executed evidence

- Full offline suite against disposable PostgreSQL17 (matching Render Blueprint major) and SQLite: **592 passed,10 skipped**,53.39seconds. A subsequent request-summary regression is covered in focused tests and the current GitHub full-suite check; use PR #10's current CI result for final count. Seven skips are SQLite counterparts of PostgreSQL-only tests; three require a private original sample, not committed.
- Real PostgreSQL17 server/container restart with a dedicated disposable volume: four entities, frozen payload SHA256, cycle usage40 and uncertain outbox survived. A mocked Resend retry accepted the identical payload once; another daily invocation returned already_sent without scanning or sending. Evidence in examples/hardening-synthetic/recovery.json and checkpoint.json. These are fake transports; no actual provider connection is implied.
- Baseline master `051e11a` storage code created/migrated an isolated PostgreSQL schema with fictional original properties, usage, cache, taxes, price history and sent outbox. All **18 legacy table content hashes** were unchanged after two current migrations and an old-code reopen/migration. Production database was not queried. Evidence: examples/hardening-synthetic/migration-audit.json.
- `git merge-base --is-ancestor bc2b3e4 HEAD` passed. `git merge-tree --write-tree master HEAD` produced a clean tree without changing branches or merging. Master/PR #9 remote heads were unchanged when inspected. Build/start commands and schedules were not edited.
- New tests include complete ingestion→normalization→dedup→original filters→mock financial enrichment→underwriting→score→PostgreSQL→report→failed delivery→reopen/retry. Other new regressions cover financial conflicts, estimated taxes, input units, zero-equity CoC, configuration mismatch, historical imports, source states, purpose allocations, snapshot/body tampering and cross-mode draft history.

## Defects fixed

1. Rent/HOA conflicts could leave a property qualified: now pending, no investment score.
2. An estimated tax above the hard limit could reject an otherwise unverified property: now pending; actual tax still obeys strict annual+$100<$2,000. Fractional years are rejected instead of truncated.
3. Unknown transport/input names and invalid rates/terms could be ignored or fail late: validated before activation. No assumptions/defaults approved.
4. Zero initial equity could produce a “complete” model with undefined CoC: explicitly incomplete.
5. Old cached/inbox listings imported into the unified tables could acquire today's discovery date: original observation date retained, not NEW merely from import.
6. Shared daily ID could retain both RS and unified report snapshots and block a safe unattempted cutover: new snapshots bind to the exact outbox body hash. Only the matching snapshot validates; ambiguous/tampered bodies fail closed. Old snapshots/history are preserved; attempted messages still freeze and require reconciliation.
7. Generic raw source-result output obscured scan state; explicit fresh/cache/skip/budget/outage/verification states and request counts now appear. “No new listings” is independent of scan success. New HTML table includes activity, price, qualification, cash flow, score, confidence and missing evidence. Source/financial details remain available.

## Complete dry-run artifact

examples/hardening-synthetic/report.html, report.txt and report.json are explicitly fictional. Four newly discovered properties, one price reduction, one screened/model-complete candidate, two pending and one rejected; a cross-source duplicate stays one entity. Example:101 Fixture St price$140,000, modeled rent$1,500, annual tax$1,500, cash flow$936/month, NOI$12,132 and after-reserve all-in yield7.269903%. All operating assumptions are marked fictional and do not become owner policy. Missing tax/rent cases remain pending; excluded ZIP fails. MLS outage and budget exhaustion are shown separately from successful fixture discovery.

Four mocked RentCast calls were made through injected fixtures;36 earlier account uses were simulated to exercise the40 ceiling. No real40-call scan happened. All default urllib provider transports are blocked by the simulation. A non-synthetic source flag is used only to exercise actual send/recovery code with the fake opener; exported artifacts are forcibly labeled synthetic. The separate production synthetic-delivery guard remains tested and unchanged. The script refuses remote database URLs, query overrides and non-test database names.

Reproduce only in an isolated LOCAL test PostgreSQL: set HARDENING_TEST_DATABASE_URL to a local database ending `_test`, run scripts/hardening_simulation.py with --output, restart ONLY that disposable database preserving its local test volume, then rerun with --recover. No production URL is accepted. Schema IDs/checkpoints are local runtime artifacts, not credentials. Existing pytest integration fixtures create/drop isolated schemas automatically.

## Remaining release gates

See PRODUCTION_ACTIVATION_RUNBOOK.md for exact existing-service order, flags, migration/rollback, cutover, outbox protection and monitoring; UNDERWRITING_DECISIONS.md for unapproved proposals. No live feed/forwarding/schema inspection, real coverage benchmark, production backup/configuration/outbox inspection or real Resend acceptance was performed. These remain required even if licenses arrive. The previously supplied original Exclusives sample was also revalidated offline after the hardening:7 tests passed (including four overlapping sanitized-template tests). No validation of a real regular-alert template is claimed.
