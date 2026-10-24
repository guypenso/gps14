# Underwriting decision sheet — proposed only, NOT approved/runtime defaults

Update: the owner has confirmed BOTH cash and financed analysis, with balanced annual-return/monthly-flow ranking. See DUAL_UNDERWRITING.md for the implemented dual policy. Expense values and financing terms below remain proposals, not approvals.

Recovered hard policy remains unchanged: latest annual property tax plus$100 strictly below$2,000, Pittsburgh polygon/ZIP exclusions, active status, price/beds/baths/area limits. No minimum yield, cash-flow or cash-on-cash threshold, financing terms or operating expense policy was recovered. Until approved inputs exist, real candidates stay Pending Verification. “Qualified” in this implementation means original screening plus a complete labeled model, not an owner-approved economic return or independent verification of every modeled input.

| Input | Recommended treatment for review | Sensitivity to review | Why / implementation basis |
|---|---|---|---|
| Annual property tax/year | Latest actual bill/source evidence; no estimated default | Test strict boundary1899.99 passes /1900 fails; evaluate latest-year discrepancies | Never substitute assessed value or older-year bill. The extra$100 is screening buffer, not an invented operating expense. |
| Market rent | Supported estimate with comparables; prefer a defensible lower-range case | Rent−10% and−20%, only explicit approved scenarios | Never infer rent from asking price. Estimate remains labeled, not “verified rent.” |
| Insurance | Property-specific annual quote; no generic dollar default recommended | Quote+25% /+50% | Coverage, condition and deductible matter; placeholder dollar budgets are not evidence. |
| Vacancy | Proposed5% of gross rent | 5–10% | Allow downtime; local operating evidence should replace proposal. |
| Maintenance | Proposed8% of gross rent | 5–12% | Age/condition dependent; excludes CapEx. |
| Capital expenditures | Proposed5% of gross rent | 5–10%, plus inspection-based immediate work | Deduct after NOI; do not hide major repairs inside maintenance. |
| Management | Proposed10% of collected rent | 8–12%; quote leasing/renewal fees separately | Current calculator basis is collected rent; additional contract fees need explicit treatment, not silent inclusion. |
| HOA | Actual documented monthly amount; zero only with evidence | Known amount and announced increases | No default assumption that omitted HOA means zero. |
| Closing costs | Transaction/financing quote | Illustrative2%,3%,5% of price converted into separate scenario dollar inputs | Dual mode converts an explicit amount OR rate once per property; no quote is invented. Keep shared and loan costs disjoint; prevent prepaid insurance/tax double-counting. |
| Renovation | Inspection/scope-based cash amount; no property-independent default | Contractor range + explicit contingency scenario | Missing budget remains missing, even if the property looks inexpensive. |
| Financing | Analyze BOTH cash and financed; provide loan down payment/LTV, annual rate, amortization years and actual costs; missing financing remains incomplete | Rate+1/+2percentage points; quote scenarios | Cash is an independent comparison, not an instruction to buy cash. Missing loan terms leave financed metrics incomplete. |
| Profitability acceptance | Owner must select minimum net yield/cash flow/CoC if intended | Compare candidate economics before choosing | No new hard economic cutoff is applied. Negative modeled cash flow is a risk, not an invented rejection rule. |

Percentages above are engineering proposals, NOT Pittsburgh market measurements or approved policy. They are not loaded into configuration. Do not copy the synthetic demonstration's insurance$1,200, maintenance5%, management8%, closing$4,500, renovation$10,000 or cash mode into production.

Calculation audit: annual gross=12×monthly rent; collected=gross×(1−vacancy). NOI=collected−tax−insurance−12×HOA−gross×maintenance−collected×management. After-reserve annual income=NOI−gross×CapEx. Net yield=after-reserve income/(price+closing+renovation). Cash flow subtracts amortized debt only for explicit financing. CoC divides annual post-debt flow by initial down payment+closing+renovation (or all-in cost for cash). Zero initial investment yields unknown CoC and an incomplete model. Loan rate is annual fractional rate; terms/rates/input names are validated. Rent and HOA conflicts make qualification pending.

New ranking uses separate cash net-yield/monthly-flow and financed CoC/monthly-flow percentiles with proposed50/50 weights; legacy yield-only scores remain historical compatibility output. Data completeness and evidence confidence remain separate (not calibrated probabilities). New qualified candidates are grouped first and ranked by score; pending and older records remain separate. Missing insurance/renovation/financing must not be hidden behind an attractive gross yield. Scenario inputs require an approval reference and retain their provenance. Property-specific costs cannot be replaced by a global quote for another property; use explicit property scenarios or withhold qualification until evidence is available.
