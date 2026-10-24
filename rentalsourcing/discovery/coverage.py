"""Measured cohort comparison. No benchmark => no coverage percentage."""
import json,statistics
from collections import Counter,defaultdict
from .models import instant


def compare(observations,*,reference=None,reference_permission=None,as_of=None,expected_sources=()):
    by=defaultdict(list)
    for row in observations:
        if as_of is None or instant(row['discovered_at'])<=instant(as_of):by[row['source']].append(row)
    for source in expected_sources:by[source]
    if reference is not None and not reference_permission:raise PermissionError('Authorized reference dataset required')
    if reference is not None and not as_of:raise ValueError('Reference snapshot timestamp required')
    relevant={r['key']:r for r in (reference or []) if r.get('relevant') is True}
    results={}
    for source,rows in by.items():
        keys={r['key'] for r in rows};others={r['key'] for s,rs in by.items() if s!=source for r in rs}
        first={};latest={}
        for row in rows:
            if row['key'] not in first or instant(row['discovered_at'])<instant(first[row['key']]['discovered_at']):first[row['key']]=row
            if row['key'] not in latest or instant(row['discovered_at'])>instant(latest[row['key']]['discovered_at']):latest[row['key']]=row
        delays=[];statuses=[];gaps=Counter()
        for key,ref in relevant.items():
            item=first.get(key)
            if item and ref.get('listed_at'):
                delay=(instant(item['discovered_at'])-instant(ref['listed_at'])).total_seconds()/3600
                if delay>=0:delays.append(delay)
            if item and ref.get('status') is not None and latest[key].get('status') is not None:statuses.append(latest[key]['status']==ref['status'])
            if not item:gaps[str(ref.get('zipcode') or 'unknown')]+=1
        # Freshness accuracy requires synchronized snapshot/cohort scope; caller retains the manifest.
        results[source]=dict(total_records=len(rows),unique_properties=len(keys),repeated_property_observations=len(rows)-len(keys),duplicate_records=len(rows)-len({r.get('observation_id') or json.dumps(r,sort_keys=True) for r in rows}),
            unique_to_source=sorted(keys-others),relevant_found=len(keys&relevant.keys()) if reference is not None else None,
            missed=sorted(relevant.keys()-keys) if reference is not None else None,
            coverage_pct=round(100*len(keys&relevant.keys())/len(relevant),2) if relevant else None,
            median_discovery_delay_hours=statistics.median(delays) if delays else None,
            reference_within24h_coverage_pct=round(100*sum(d<=24 for d in delays)/sum(bool(r.get('listed_at')) for r in relevant.values()),2) if any(r.get('listed_at') for r in relevant.values()) else None,
            found_within24h_pct=round(100*sum(d<=24 for d in delays)/len(delays),2) if delays else None,
            status_accuracy_pct=round(100*sum(statuses)/len(statuses),2) if statuses else None,
            status_comparable_count=len(statuses),delay_comparable_count=len(delays),
            missing_financial_count=sum(not r.get('financial_complete',False) for r in first.values()),geographic_gaps=dict(gaps))
    allkeys={r['key'] for rows in by.values() for r in rows}
    return dict(reference_authorized=bool(reference_permission),reference_relevant_count=len(relevant) if reference is not None else None,
        sources=results,union_coverage_pct=round(100*len(allkeys&relevant.keys())/len(relevant),2) if relevant else None,
        targets={'coverage_pct':95,'delay_hours':24,'status':'proposed_not_demonstrated_by_fixture_tests'},
        limitation='Coverage is only against the supplied authorized cohort, never a claim of all MLS inventory.')


def from_store(store,now,*,reference=None,reference_permission=None,as_of=None):
    rows=[]
    for source,key,processed,raw,oid in store.execute('SELECT source,entity_id,processed_at,payload,id FROM discovery_observations').fetchall():
        p=json.loads(raw);fields=p['fields'];rows.append(dict(observation_id=oid,source=source,key=key,discovered_at=processed,status=fields.get('status'),financial_complete=all(fields.get(k) is not None for k in ('annual_tax','monthly_rent'))))
    result=compare(rows,reference=reference,reference_permission=reference_permission,as_of=as_of,expected_sources=[r[0] for r in store.execute('SELECT name FROM discovery_sources').fetchall()])
    result['operational_ingest_attempts_cumulative']={name:dict(attempts=a,duplicates=d) for name,a,d in store.execute('SELECT source,attempts,duplicates FROM discovery_source_metrics').fetchall()}
    return result
