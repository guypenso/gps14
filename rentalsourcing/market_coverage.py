"""Observed source footprint, not an estimate of total active market inventory."""
import hashlib
import json
from pathlib import Path

from shapely.geometry import Point, shape

from .collector import LIVE
from .core import number


def observed_coverage(store, boundary, listing_rows, neighborhoods_path):
    """Compare stored source identities against the pinned City neighborhood map.

    A zero is only a zero in our observed inventory. It does not establish that
    a saved search covers the area, or that the area has zero active listings.
    """
    path = Path(neighborhoods_path)
    manifest = json.loads(path.with_suffix('.source.json').read_text())
    raw = path.read_bytes()
    if (manifest.get('source') != 'https://data.wprdc.org/dataset/neighborhoods2'
            or manifest.get('publisher') != 'City of Pittsburgh'
            or hashlib.sha256(raw).hexdigest() != manifest.get('sha256')):
        raise ValueError('Neighborhood polygon provenance mismatch')
    features = json.loads(raw)['features']
    if len(features) != 90 or manifest.get('features') != 90:
        raise ValueError('Incomplete neighborhood polygon inventory')
    regions = [(f['properties']['hood'], shape(f['geometry'])) for f in features]
    if len({name for name, _ in regions}) != 90:
        raise ValueError('Non-unique neighborhood names')
    counts = {name: {'rentcast': set(), 'realscout': set(), 'latest_rentcast': None,
                     'latest_realscout': None} for name, _ in regions}
    counts['Bellevue Borough'] = {'rentcast': set(), 'realscout': set(),
                                  'latest_rentcast': None, 'latest_realscout': None}
    dimensions = {source: {field: {} for field in ('zip_code', 'asking_price_band', 'bedrooms',
                                                   'listing_status', 'alert_type')}
                  for source in ('rentcast', 'realscout')}
    unlocated = {'rentcast': 0, 'realscout': 0}
    outside_approved = {'rentcast': 0, 'realscout': 0}
    ambiguous = {'rentcast': 0, 'realscout': 0}

    def observe(identity, property_data, seen_at, source, alert_type=None):
        x, y = property_data.get('longitude'), property_data.get('latitude')
        if not number(x) or not number(y):
            unlocated[source] += 1
            return
        state, _ = boundary.evaluate(property_data)
        if state == 'Rejected':
            outside_approved[source] += 1
            return
        if state != 'Candidate':
            ambiguous[source] += 1
            return
        point = Point(float(x), float(y))
        if boundary.bellevue.geometry.covers(point):
            name = 'Bellevue Borough'
        else:
            names = [name for name, geom in regions if geom.covers(point)]
            if len(names) != 1:
                ambiguous[source] += 1
                return
            name = names[0]
        cell = counts[name]
        cell[source].add(identity)
        price = property_data.get('price')
        price_band = ('unknown' if not number(price) else
                      'up_to_100000' if price <= 100000 else
                      '100001_to_150000' if price <= 150000 else
                      '150001_to_200001' if price <= 200001 else 'over_200001')
        values = {'zip_code': str(property_data.get('zipCode') or 'unknown').split('-')[0],
                  'asking_price_band': price_band,
                  'bedrooms': str(property_data.get('bedrooms') or 'unknown'),
                  'listing_status': str(property_data.get('status') or 'unknown'),
                  'alert_type': str(alert_type or 'unknown')}
        for field, value in values.items():
            dimensions[source][field].setdefault(value, set()).add(identity)
        key = 'latest_' + source
        if seen_at and (cell[key] is None or cell[key] < seen_at):
            cell[key] = seen_at

    for identity, raw, seen_at in store.execute(
            'SELECT id,payload,observed_at FROM properties WHERE source=?', (LIVE,)).fetchall():
        observe(identity, json.loads(raw), seen_at, 'rentcast')
    for row in listing_rows:
        observe(row['source_id'], row['property'], row.get('observed_at'), 'realscout',
                row.get('raw_listing_status'))

    matrix = []
    for name in sorted(counts):
        c = counts[name]
        matrix.append({'neighborhood': name,
                       'rentcast_observed': len(c['rentcast']),
                       'realscout_observed': len(c['realscout']),
                       'both_sources': len(c['rentcast'] & c['realscout']),
                       'realscout_only': len(c['realscout'] - c['rentcast']),
                       'latest_rentcast': c['latest_rentcast'],
                       'latest_realscout': c['latest_realscout']})
    return {'state': 'observed_inventory_only', 'market_denominator': None,
            'market_coverage_percent': None,
            'search_configuration_verified': False,
            'source_dataset': manifest['source'],
            'polygon_retrieval_method': manifest.get('retrieval_method'),
            'neighborhoods': matrix,
            'zero_observed_neighborhoods': [r['neighborhood'] for r in matrix
                                            if not r['rentcast_observed'] and not r['realscout_observed']],
            'unlocated_source_identities': unlocated,
            'outside_approved_geography': outside_approved,
            'ambiguous_geography': ambiguous,
            'approved_geography_observed_dimensions': {
                source: {field: {value: len(identities) for value, identities in sorted(entries.items())}
                         for field, entries in fields.items()}
                for source, fields in dimensions.items()},
            'limitations': 'No independent current-listing denominator or authenticated RealScout saved-search configuration; absence from a partial source scan does not prove a missing listing.'}
