"""Existing hard criteria, separate completeness/confidence and transparent profitability."""
import json
from .repository import resolution
from .finance import Input,underwriting,REQUIRED
from ..core import verification,tax_verification

CRITICAL_CONFLICTS={'price','status','bedrooms','bathrooms','squareFootage','latitude','longitude','annual_tax','tax_year','monthly_rent','hoa_monthly'}


def evaluate(store,entity_id,boundary,now,*,assumptions=None,financing=None):
    info=resolution(store,entity_id,now);fields=info['fields']
    row=store.execute('SELECT address,version,first_seen FROM discovery_entities WHERE id=?',(entity_id,)).fetchone()
    if not row:raise ValueError('Unknown property')
    status,reasons=verification(dict(fields,id=entity_id,formattedAddress=row[0]),boundary)
    original=status;tax=None
    if fields.get('annual_tax') is not None and fields.get('tax_year') is not None:
        tax_verified=info['provenance'].get('annual_tax',{}).get('kind')=='verified_source'
        if not tax_verified:reasons.append('annual_tax_is_only_an_estimate')
        year=fields['tax_year'];tax_state,tax,why=tax_verification({'propertyTaxes':{str(int(year)):{'year':int(year),'total':fields['annual_tax']}}},now.date())
        if status!='Rejected' and tax_state=='Rejected' and tax_verified:status='Rejected';reasons.append(why)
        elif tax_state=='Pending Verification':reasons.append(why)
        elif status=='Candidate' and tax_verified:original='Qualified'
    else:reasons.append('annual_tax_and_year_required')
    conflicts=CRITICAL_CONFLICTS&info['conflicts'].keys()
    if conflicts:reasons+=['conflicting_'+f for f in sorted(conflicts)]
    if status=='Rejected':original='Rejected'
    if conflicts:original='Pending Verification'
    inputs=dict(assumptions or {})
    for name in ('monthly_rent','annual_tax','hoa_monthly'):
        if name in fields:
            p=info['provenance'][name];inputs[name]=Input(fields[name],p['kind'],p['source'])
    financial=underwriting(fields.get('price'),inputs,financing=financing)
    reasons+=['financial_missing_'+x for x in financial.get('missing',[]) ]
    if status!='Rejected' or conflicts:status='Pending Verification' if reasons else 'Qualified'
    if fields.get('estimated_value') and fields.get('price'):
        financial['metrics']['asking_discount_to_estimated_value_pct']=round(100*(fields['estimated_value']-fields.get('price',0))/fields['estimated_value'],4)
    required={'price','bedrooms','bathrooms','squareFootage','status','state','zipCode','latitude','longitude','propertyType','annual_tax','tax_year','monthly_rent','hoa_monthly'}
    completeness=round(100*len(required&fields.keys())/len(required),2)
    evidence=info['provenance'];quality={'mls':1.0,'public_records':1.0,'rentcast':0.8,'realscout':0.6}
    confidence=round(100*sum(quality[p['source_kind']]*(0.65 if p['kind']=='third_party_estimate' else 1) for p in evidence.values())/len(required),2) if evidence else 0
    confidence=max(0,min(100,confidence-10*len(conflicts)))
    financial_confidence='incomplete' if not financial['complete'] else 'modeled_with_assumptions_or_estimates' if financial.get('assumptions') or financial.get('estimates') else 'source_backed_inputs'
    return dict(id=entity_id,address=row[0],version=row[1],discovered_at=row[2],fields=fields,
        status=status,original_screening=original,reasons=sorted(set(reasons)),tax=tax,financial=financial,
        investment_score=round(financial['score'],4) if status!='Rejected' and financial.get('score') is not None and not conflicts else None,
        data_completeness_pct=completeness,evidence_confidence_pct=confidence,financial_confidence=financial_confidence,
        latest_verification=max((p['observed_at'] for p in evidence.values()),default=None),
        provenance=evidence,conflicts=info['conflicts'],stale_evidence=info['stale_evidence'],sources=info['sources'],synthetic=info['synthetic'],
        risks=['no_profitability_cutoff_approved']+(['negative_modeled_cash_flow'] if (financial['metrics'].get('cash_flow_monthly') or 0)<0 else []),
        next_research='Resolve: '+', '.join(sorted(set(reasons))) if reasons else 'Confirm rent comparables, physical condition and modeled assumptions before an offer')
