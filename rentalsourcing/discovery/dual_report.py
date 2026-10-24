"""Cash/financed comparison rendered from a frozen unified snapshot; no transport changes."""
import html,json


def fmt(value):return f'{value:,.2f}' if isinstance(value,(int,float)) and not isinstance(value,bool) else 'Missing'


def render(data):
    from .report import scan_outcomes
    esc=html.escape;lines=[f"RentalSourcing | {data['day']} | Cash and financed analysis",
      'SYNTHETIC — EVERY PROPERTY AND VALUE IS FICTIONAL; NOT INVESTMENT RECOMMENDATIONS' if data['synthetic'] else 'Modeled strategy comparisons; assumptions and estimates are not verified investment performance.',
      data['coverage_statement'],f"New listings: {data['counts']['new']} | Price reductions: {data['counts']['price_reductions']} | Pending: {data['counts']['pending']}",
      'No new listings discovered.' if data['no_new_listings'] else 'New refers to this report window; stored listings are labeled separately.',
      'Proposed ranking weights; not permanently approved.' if not data.get('ranking_approval_id') else 'Ranking policy approval: '+data['ranking_approval_id'],
      'Weights: '+str(100*data['rankings']['weights']['annual_return'])+'% annual return / '+str(100*data['rankings']['weights']['monthly_cash_flow'])+'% monthly cash flow.',data['rankings']['method'], 'No profitability minimum was approved. Negative results remain visible with risk labels.']
    blocks=['<h1>RentalSourcing — dual strategy</h1>','<p>'+esc(lines[1])+'</p>']
    for row in scan_outcomes(data):lines.append('Source '+row['source']+': '+row['state']+f"; requests={row['requests']}")
    if not data.get('source_results'):lines.append('No discovery attempt recorded; this analysis uses supplied/stored evidence.')
    for source in data['sources']:lines.append(f"{source['name']}: last success={source['last_success']}; last attempt={source['last_attempt']}; last-attempt error={source['error']}; stale={source['stale']}")
    if 'api' in data:lines.append('API accounting: '+json.dumps(data['api'],sort_keys=True))
    def top(title,rows):
        lines.append(title);blocks.append('<h2>'+esc(title)+'</h2>')
        # Rank against the disclosed full cohort; daily highlights only newly observed events.
        highlights=[r for r in rows if r['new'] or r['price_reduction'] or r['seen_this_window']][:10]
        if not highlights:lines.append('No complete, relevant scenarios with new activity.');blocks.append('<p>No complete scenarios with new activity.</p>')
        for r in highlights:
            label='PROVISIONAL SCENARIO' if r['provisional'] else 'MODELED COMPARISON — not a buy recommendation'
            text=f"{r['address']} | score {r['score']:.2f}/100 | annual return {r['annual_return']:.2f}% ({r['annual_return_metric']}) | monthly flow ${r['monthly_cash_flow']:,.2f} | initial cash ${r['initial_cash']:,.2f} | {label} | return percentile {r['return_percentile']:.2f}; cash-flow percentile {r['cash_flow_percentile']:.2f}; n={r['cohort_size']} | {r['reasons']}"
            lines.append(text);blocks.append('<p>'+esc(text)+'</p>')
    top('Top Cash Opportunities — scenario comparisons',data['rankings']['cash'])
    for name,rows in data['rankings']['financed'].items():top('Top Financed Opportunities — '+name+' scenario comparisons',rows)
    rows=[('Purchase price','purchase_price'),('Initial invested capital','total_initial_cash'),('Shared acquisition closing cost','closing_cost'),('Renovation cost','renovation_cost'),
          ('Down payment','down_payment'),('Loan principal','loan_principal'),('LTV (%)','loan_to_value'),('Interest rate (annual %)','interest_rate'),('Amortization years','loan_years'),('Loan closing costs (additional)','loan_closing_cost'),
          ('Monthly gross rent','monthly_gross_rent'),('Annual gross rent','annual_gross_rent'),('Annual effective rent after vacancy','annual_effective_rent'),('Monthly effective rent after vacancy','monthly_effective_rent'),('Annual operating expenses (after vacancy)','annual_operating_expenses'),('Annual NOI','noi_annual'),('Annual CapEx reserves','annual_capex_reserve'),
          ('Annual after-reserve income before debt','annual_after_reserve_cash_flow'),('Monthly debt service','debt_service_monthly'),('Monthly net cash flow after reserves/debt','cash_flow_monthly'),('Annual net cash flow after reserves/debt','annual_net_cash_flow'),
          ('Property-level net unlevered yield (%)','net_yield_pct'),('Cash-on-cash return (%)','cash_on_cash_pct'),('DSCR — NOI / annual debt service','dscr')]
    for p in data['properties']:
        acq=p['acquisition'];activity=' + '.join(t for flag,t in ((p['new'],'NEW'),(p['price_reduction'],'PRICE REDUCTION')) if flag) or 'PREVIOUSLY SEEN'
        heading=f"{activity} | {p['address']} | {p['status']}"
        lines+=[heading,f"{p['fields'].get('bedrooms')} beds / {p['fields'].get('bathrooms')} baths / {p['fields'].get('squareFootage')}sqft | tax ${p['fields'].get('annual_tax')} year {p['fields'].get('tax_year')}",
                f"Discovered {p['discovered_at']}; verified/evidence timestamp {p['latest_verification']}; completeness {p['data_completeness_pct']}%; evidence indicator {p['evidence_confidence_pct']}% (not a probability)",
                'Sources: '+', '.join(p['sources']),'Field conflicts: '+json.dumps(p['conflicts'],sort_keys=True),'Source evidence: '+json.dumps(p['provenance'],sort_keys=True),'Reasons/missing evidence: '+(', '.join(p['reasons']) or 'Original screening passed; models complete; review evidence and assumptions.')]
        blocks.append('<h2>'+esc(heading)+'</h2><p>'+esc('Evidence indicator '+str(p['evidence_confidence_pct'])+'% (not a probability); missing/review: '+(', '.join(p['reasons']) or 'review modeled assumptions')+'; risks: '+', '.join(p['risks']))+'</p>')
        for name,financed in acq['financed'].items():
            cash=acq['cash'];table=['<table style="border-collapse:collapse" border="1" cellpadding="6"><tr><th>Metric</th><th>ALL-CASH</th><th>FINANCED — '+esc(name)+'</th></tr>']
            lines.append('ALL-CASH versus FINANCED — '+name)
            table.append('<tr><td>Scenario input completeness (%)</td><td>'+fmt(cash['data_completeness_pct'])+'</td><td>'+fmt(financed['data_completeness_pct'])+'</td></tr>')
            for title,key in rows:
                a=fmt(cash['metrics'].get(key));b=fmt(financed['metrics'].get(key))
                if key in {'down_payment','loan_principal','loan_to_value','interest_rate','loan_years','loan_closing_cost','dscr'}:a='N/A (cash)'
                if key in {'loan_to_value','interest_rate'}:
                    value=financed['metrics'].get(key);b=f'{100*value:.4f}%' if value is not None else 'Missing'
                lines.append(f'{title}: CASH {a} | FINANCED {b}')
                table.append('<tr><td>'+esc(title)+'</td><td>'+a+'</td><td>'+b+'</td></tr>')
            blocks.append(''.join(table)+'</table>')
            for label,scenario in [('CASH',cash),('FINANCED '+name,financed)]:
                lines += [label+' status: '+scenario['qualification_status'],label+' missing: '+', '.join(scenario['missing']),label+' risks: '+', '.join(scenario['risks']),label+' inputs/provenance: '+json.dumps(scenario['inputs'],sort_keys=True)]
        for link in p['links']:
            lines.append(link['source']+': '+link['url']);blocks.append('<p><a href="'+esc(link['url'],quote=True)+'">'+esc(link['source']+' listing')+'</a></p>')
        if not p['links']:lines.append('Listing link missing; do not fabricate one.')
        lines.append('Sensitivity tests — hypothetical, not predictions:')
        stress_rows=['<table border="1" cellpadding="5"><tr><th>Hypothetical stress</th><th>Cash monthly / yield</th><th>Financed monthly / CoC by profile</th></tr>']
        for name,result in p['sensitivities'].items():
            comparison=result.get('comparison',{});cm=comparison.get('cash',{}).get('metrics',{})
            loans=comparison.get('financed',result.get('financed',{}));fm='; '.join(profile+': $'+fmt(value['metrics'].get('cash_flow_monthly'))+' / '+fmt(value['metrics'].get('cash_on_cash_pct'))+'%' for profile,value in loans.items()) or 'Incomplete'
            cash_text=('$'+fmt(cm.get('cash_flow_monthly'))+' / '+fmt(cm.get('net_yield_pct'))+'%') if cm else 'Financed only' if result['applies_to']=='financed_only' else 'Incomplete'
            text=name+': CASH '+cash_text+'; FINANCED '+fm;lines.append(text);stress_rows.append('<tr><td>'+esc(name)+'</td><td>'+esc(cash_text)+'</td><td>'+esc(fm)+'</td></tr>')
        blocks.append('<h3>Hypothetical sensitivity tests</h3>'+''.join(stress_rows)+'</table>')
    lines.append('Definitions: NOI excludes CapEx and debt. DSCR uses NOI before CapEx / scheduled annual principal and interest. Net property yield excludes financing fees; financed CoC includes them in initial cash. No appreciation, principal-paydown return or tax benefits assumed.')
    if data['omitted']:lines.append(f"{data['omitted']} further records retained in the private database.")
    text='\n'.join(line.rstrip() for line in lines);blocks.append('<h2>Evidence, definitions and full calculation details</h2><pre style="white-space:pre-wrap">'+esc(text)+'</pre>')
    return dict(subject=f"RentalSourcing | {data['day']} | Cash and financed comparisons",text=text,html='<html><body>'+''.join(blocks)+'</body></html>')
