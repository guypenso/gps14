> Current product policy: listing-only MVP is the default. Advanced underwriting is retained but disabled. See [MVP_REPORTING.md](MVP_REPORTING.md); financial assumptions are not MVP eligibility or activation prerequisites.

# Dual acquisition underwriting — implementation and activation contract

The owner approved comparing BOTH all-cash and financed acquisitions and balancing annual percentage return with monthly dollar cash flow. Expense values, financing terms and permanent ranking weights have NOT been approved. No provider, schedule, hard filter or production setting is changed. No migration is needed: versioned scenario results, provenance and ranking context are stored in existing discovery_reports snapshots and the original outbox.

## Model and units

Both strategies share asking price, supported rent, vacancy, annual tax, insurance, verified monthly HOA, maintenance, CapEx and property-specific renovation. No omitted expense becomes zero. All money is USD; percentages are fractions in inputs and percentage points in outputs. Monthly rent/HOA are annualized exactly once. Rates/values must be finite, nonnegative; loan amortization is positive integer years, fixed rate; LTV must be>0 and<=1 (100% is permitted as a scenario, flagged zero down). No minimum down payment, DSCR, profitability or yield threshold is invented.

- Annual gross rent=12×monthly rent; effective rent=gross×(1−vacancy).
- Operating expenses=annual tax+insurance+12×HOA+gross×maintenance+effective rent×management. Vacancy is already removed from income and is not charged again as an expense.
- NOI=effective rent−operating expenses, BEFORE CapEx and debt.
- Annual after-reserve income=NOI−gross×CapEx; CapEx is NOT also deducted inside NOI.
- Cash total investment=price+shared acquisition closing costs+renovation. Monthly cash flow=after-reserve income/12. Cash net yield and CoC=after-reserve annual income/total investment.
- Financed principal=price×LTV or price×(1−down-payment fraction). Monthly principal-and-interest payment=P×r/(1−(1+r)^−n), monthly rate r=annual rate/12, n=years×12. At zero interest, payment=P/n.
- Financed initial cash=down payment+shared closing+renovation+additional loan closing costs. Annual net cash flow=after-reserve income−12×payment. Monthly flow=annual/12; CoC=annual flow/initial cash. Zero initial cash makes CoC undefined and the scenario incomplete, never infinite.
- DSCR=NOI/annual scheduled principal-and-interest service, BEFORE CapEx. It differs from after-reserve coverage. DSCR<1 and negative cash flow are labeled risks, not rejection filters.
- Property-level unlevered yield uses the same property capital/income in both strategies, excluding loan fees and leverage. Financed CoC is a different metric, never labeled property yield. No appreciation, sale proceeds, principal-paydown return, income-tax benefit or refinancing assumption is included.

`closing_cost` (absolute amount) and `closing_cost_rate` (fraction×price) are mutually exclusive. They mean NON-FINANCING transaction costs, excluding items already counted as annual taxes/insurance/maintenance. `loan_closing_cost` is additional cash-paid loan cost, required even when a confirmed0. It is counted once in financed initial equity, never in NOI or cash acquisition cost. If a quote bundles prepaids/loan fees, it must be classified into disjoint components before entry; code cannot infer overlap hidden inside a quoted amount. Additional recurring loan charges/PMI, escrow funding, financed fees, points amortization, balloons and interest-only loans are not modeled: verify the actual quoted product fits the fixed principal-and-interest scope before relying on a live financed comparison; do not describe this as a lender disclosure or full first-year liquidity forecast.

## Reliability and qualification

Existing geographic/status/price/bed/bath/area/ZIP rules and latest verified annual tax+$100 strictly below$2,000 remain unchanged. Actual annual tax1899.99 passes;1900 fails. Estimated taxes, unknown latest bills or missing HOA do not qualify by assumption. Conflicting rent/tax/HOA or other critical screening evidence prevents ranking/qualification. Noncritical source discrepancies remain visible through confidence/risk information.

Property-specific insurance must be a quote or supported estimate, not a global invented dollar default. Renovation needs property-specific evidence or an explicitly hypothetical scenario; missing renovation is never zero. A supported rent estimate stays labeled an estimate. Replacing absent rent with a generic assumption cannot fully qualify a property.

`Input.kind` distinguishes verified_source (documented source value, not a claim of independent inspection), third_party_estimate, assumption (owner-approved model input with approval reference), hypothetical (what-if, never verified). Suggested inputs are NOT loaded into the live baseline. Hypothetical configuration without owner review references is allowed only for entirely synthetic sources; live-source hypothetical baselines require an explicit review reference and remain Pending Verification. Automatically generated sensitivity cases are always hypothetical, never baseline inputs. Snapshot storage retains full base provenance once and records each sensitivity's explicit changed/removed inputs plus results and a baseline reference; it does not duplicate every source/quote in every stress case. A nonempty approval string must refer to actual consent, not be fabricated to bypass review.

Each candidate has separate cash and named financed scenario completeness, provenance, risks and qualification. The overall dual result is Pending if required financing or evidence is missing, even when cash math is available. A complete cash scenario can receive a PROVISIONAL comparison rank while financing is incomplete. No fictional model produces an approved recommendation. No missing metric is normalized as zero. New dual reports have no single investment_score; the legacy financial view is explicitly compatibility-only, not the source for new rankings.

## Ranking policy — proposed50/50, not an economic hurdle

Cash cohort: annual AFTER-RESERVE NET YIELD percentage and monthly cash flow. Financed cohorts: annual AFTER-DEBT CASH-ON-CASH percentage and monthly after-debt flow, separately for EACH named loan profile. Never pool cash yields with leveraged CoC or pick the highest-leverage profile as a single winning score.

For each metric, score its tied empirical percentile:100×(number strictly below+(tie count−1)/2)/(cohort size−1). A singleton or all-tied cohort receives50. Balanced score=.5×annual-return percentile+.5×monthly-flow percentile, or explicit configured weights that must be nonnegative and sum to1. Thus percentages and dollars are never directly added. Profile terms, initial capital, return metric and cohort size are disclosed. Equal scores are ties; canonical property ID is only the deterministic display tie-breaker.

Cohorts include only complete relevant scenarios whose original hard screening passes, with no critical conflicts. Hypothetical/otherwise pending comparisons are labeled PROVISIONAL. Daily highlights require new/price-change/other observed activity; older stored comparisons are not NEW. Percentiles are relative, can change when the cohort changes, lose absolute distance information and can rank a negative-return property highest in a weak cohort. A score0 is not zero return;100 is not guaranteed profitability or a probability. Data completeness and evidence confidence remain separate. The proposed weights are labeled unapproved until UNIFIED_RANKING_APPROVAL_ID records actual approval.

## Sensitivities

Named, non-mutating what-ifs: rent−10%/−20%; vacancy+5percentage points; maintenance high=max(12%,1.25×base), CapEx high=max(10%,1.25×base), separately and combined; insurance+25%; renovation+25%; closing costs2%/3%/5%, replacing rather than adding the baseline representation. Loan interest+1/+2percentage points apply ONLY to financing. These are stress/review inputs, NOT market predictions or new permanent defaults. Missing underlying rent/insurance/renovation/loan terms stays incomplete. A proportional increase of an explicitly zero renovation budget remains zero, not an invented floor. Unsupported stress values are reported as incomplete, not silently clipped.

Proposed operating review baseline: vacancy5%, maintenance8% gross, CapEx5% gross, management10% collected. Review ranges:5–10%,5–12%,5–10%,8–12%, respectively. The synthetic generator uses these as hypothetical values only. No interest, LTV, term, loan cost, rent, insurance or renovation number is supplied automatically in production.

## Existing configuration, additive fields

New unified reports default to dual-v1, implementing the confirmed comparison policy. All existing discovery/provider/daily flags remain default OFF. ENABLE_DUAL_UNDERWRITING changes ONLY analysis/rendering, not provider activity; false is a legacy-compatibility option, not the policy for new production reports. UNIFIED_FINANCING_MODE remains for legacy snapshots; it does not suppress either new strategy. Old reports without a version keep their original renderer and frozen request. No historical financial report is rewritten.

- `UNIFIED_ASSUMPTIONS_JSON`: shared Input objects; no proposed numbers auto-populated. Use approval references for actual approved operating/cost assumptions.
- `UNIFIED_PROPERTY_INPUTS_JSON`: canonical `property:<hash>`→Input map for property-specific insurance/renovation and supported costs. Source observations take precedence for rent/tax/HOA. Never use a quote for another property.
- `UNIFIED_FINANCING_SCENARIOS_JSON`: name→Input map with LTV OR down_payment_rate, interest_rate, loan_years, loan_closing_cost. No loan defaults; empty input gives an incomplete base scenario.
- `UNIFIED_PROPERTY_FINANCING_JSON`: canonical property ID→named profile→term/cost Input overrides for actual property quotes. Unknown profile names fail. A local LTV replaces the alternative down-payment representation and vice versa. No term is silently discarded. Non-financing operating inputs do not belong in these maps.
- `UNIFIED_PRIMARY_FINANCING_SCENARIO`: default name base; choose explicitly when using other profile names. All profiles remain separately displayed/ranked.
- `UNIFIED_RANKING_WEIGHTS_JSON`: default proposed `{"annual_return":0.5,"monthly_cash_flow":0.5}`; valid input weights sum to1. `UNIFIED_RANKING_APPROVAL_ID` remains empty until actual approval.

Each Input is `{value,kind,source,approval_id}`; approval_id is required for assumptions, and for live-source hypothetical scenarios. Property-specific quote terms can be verified_source/third_party_estimate with a real source reference. Profiles are models, not assertions of lender qualification. Different terms by property remain visible in side-by-side tables and provenance; comparisons never hide initial equity requirements.

The frozen daily snapshot includes both strategies, sensitivities, ranking weights and provenance. Changed loan/property inputs, modeled eligibility or ranking policy invalidate an unsent dual snapshot; an attempted body still requires reconciliation. Existing outbox IDs, Resend idempotency, synthetic-delivery blocking, retries and provider quotas are unchanged. No new API request is needed to compute the second strategy or sensitivity cases.

## Executed artifacts and minimal activation steps

`scripts/dual_underwriting_dry_run.py --output <directory>` blocks default HTTP transports, uses in-memory SQLite and sends nothing. Committed examples/dual-synthetic/report.html/.txt/.json show three comparable fictional properties, two financing profiles, a fourth incomplete property and full sensitivity tables. All remain Pending; no hypothetical value is presented as a verified Pittsburgh investment result.

Before production: review proposed expenses/weights; supply supported rents, actual tax/HOA and property-specific insurance/renovation; approve named loan terms/costs or keep financing pending; install the reviewed JSON on existing services during an authorized dormant release; review one no-send dual report and confirm costs are disjoint. Then use the existing controlled activation/outbox runbook with explicit deployment/delivery approval. No MLS licensing question, new service, database migration or provider activation is needed for this financial code.
