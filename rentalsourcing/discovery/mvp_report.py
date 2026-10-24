"""Concise listing-only email. No financial model or investment scoring."""
import html
from .report import scan_outcomes


def render(data):
    def amount(value):return f'${value:,.2f}' if isinstance(value,(int,float)) else 'Missing'
    def value(v):return 'Missing' if v is None else str(v)
    qualified=[p for p in data['properties'] if p['status']=='Qualified']
    groups=[('New qualifying listings',[p for p in qualified if p['new']]),
            ('Qualifying price reductions',[p for p in qualified if p['price_reduction'] and not p['new']]),
            ('Newly verified listings',[p for p in qualified if p['newly_qualified'] and not p['new'] and not p['price_reduction']]),
            ('Pending Verification',[p for p in data['properties'] if p['status']=='Pending Verification' and p['seen_this_window']])]
    banner='SYNTHETIC OFFLINE EXAMPLE — ALL PROPERTY VALUES AND LINKS ARE FICTIONAL' if data['synthetic'] else 'Listing screening against your original hard filters.'
    lines=[f"RentalSourcing | {data['day']}",banner]
    if not any(rows for _,rows in groups[:3]):lines.append('No new or newly price-reduced qualifying properties in this report window.')
    if data['no_new_listings']:lines.append('No new listings discovered. Previously stored listings are not a fresh scan.')
    summary='\n'.join(lines)
    blocks=[]
    for title,rows in groups:
        lines+=['',title+f' ({len(rows)})'];cards=[]
        for p in rows:
            f=p['fields'];tax=p['tax'];verified=tax is not None and p['provenance'].get('annual_tax',{}).get('kind')=='verified_source'
            details=[p['address'],
                f"Asking {amount(f.get('price'))} | {value(f.get('bedrooms'))} beds | {value(f.get('bathrooms'))} baths | {value(f.get('squareFootage'))} sqft | ZIP {value(f.get('zipCode'))}",
                'Latest verified annual tax: '+(amount(tax['annual'])+f" ({tax['year']})" if verified else 'Missing / unverified'),
                f"Status: {value(f.get('status'))} | Source: {', '.join(p['sources'])} | First discovered: {p['discovered_at']}"]
            if p['price_reduction']:
                reductions=p.get('price_changes',[])
                details.append('Price reduction observed'+(' | '+', '.join(amount(x['price']) for x in reductions) if reductions else ''))
            if p['reasons']:details.append('Missing / unresolved: '+', '.join(p['reasons']))
            else:details.append('All original hard filters verified. Investment analysis remains your responsibility.')
            if not p['links']:details.append('Original listing link not supplied by source.')
            lines+=details+[link['source']+': '+link['url'] for link in p['links']]+['']
            links=' '.join('<a href="'+html.escape(link['url'],quote=True)+'">'+html.escape(link['source'])+' listing</a>' for link in p['links'])
            cards.append('<article style="padding:14px;border:1px solid #ddd;border-radius:8px;margin:10px 0">'+''.join('<p style="margin:6px 0;overflow-wrap:anywhere">'+html.escape(x)+'</p>' for x in details)+links+'</article>')
        blocks.append('<h2>'+html.escape(title)+f' ({len(rows)})</h2>'+(''.join(cards) or '<p>None.</p>'))
    outcomes=scan_outcomes(data)
    status='Discovery run: '+('; '.join(x['source']+': '+x['state'].replace('_',' ') for x in outcomes) or 'no discovery attempt recorded; scan skipped')
    lines+=['System status',status,f"Listings evaluated: {data['counts']['total']} | New: {data['counts']['new']} | Qualifying: {data['counts']['qualified']} | Pending verification: {data['counts']['pending']}"]
    for source in data['sources']:
        lines.append(f"{source['name']}: last successful check {source['last_success'] or 'never'}; {'disabled' if not source['enabled'] else 'stale' if source['stale'] else 'current'}")
    if 'api' in data:
        api=data['api'];lines.append(f"RentCast internal budget: {api['used']} used, {api['remaining']} remaining (cycle ends {api['cycle_end']}).")
    if data['omitted']:lines.append(f"{data['omitted']} additional stored records omitted; retained in the database.")
    return dict(subject=f"RentalSourcing | {data['day']} | {sum(len(rows) for _,rows in groups[:3])} qualifying listing updates",text='\n'.join(lines),
        html='<html><head><meta name="viewport" content="width=device-width, initial-scale=1"></head><body style="font-family:Arial,sans-serif;max-width:640px;margin:auto;padding:12px;line-height:1.5"><h1>RentalSourcing</h1><p>'+html.escape(summary).replace('\n','<br>')+'</p>'+''.join(blocks)+'<h2>System status</h2><p>'+html.escape('\n'.join(lines[lines.index('System status')+1:])).replace('\n','<br>')+'</p></body></html>')
