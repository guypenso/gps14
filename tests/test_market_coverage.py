import json
from pathlib import Path

import pytest
from shapely.geometry import shape

from rentalsourcing.collector import LIVE
from rentalsourcing.geography import load_boundary
from rentalsourcing.market_coverage import observed_coverage


DATA = Path(__file__).resolve().parents[1] / 'data'


def test_observed_neighborhood_matrix_keeps_sources_and_unknowns_separate(store, clock):
    boundary = load_boundary(DATA / 'pittsburgh.geojson')
    features = json.loads((DATA / 'pittsburgh-neighborhoods-wprdc.geojson').read_text())['features']
    morning = next(f for f in features if f['properties']['hood'] == 'Morningside')
    point = shape(morning['geometry']).representative_point()
    property_data = {'longitude': point.x, 'latitude': point.y, 'zipCode': '15206'}
    assert boundary.evaluate(property_data)[0] == 'Candidate'
    store.save_property('rentcast-1', property_data, 'Pending Verification', [], LIVE, 1, clock().isoformat())
    rows = [{'source_id': 'rentcast-1', 'property': property_data, 'observed_at': clock().isoformat()},
            {'source_id': 'realscout-2', 'property': property_data, 'observed_at': clock().isoformat()},
            {'source_id': 'realscout-unlocated', 'property': {'zipCode': '15206'}, 'observed_at': clock().isoformat()}]
    result = observed_coverage(store, boundary, rows, DATA / 'pittsburgh-neighborhoods-wprdc.geojson')
    cell = next(c for c in result['neighborhoods'] if c['neighborhood'] == 'Morningside')
    assert (cell['rentcast_observed'], cell['realscout_observed'], cell['both_sources'], cell['realscout_only']) == (1, 2, 1, 1)
    assert result['unlocated_source_identities']['realscout'] == 1
    assert result['market_denominator'] is None and result['market_coverage_percent'] is None
    assert len(result['neighborhoods']) == 91


def test_unverified_polygon_export_cannot_supply_coverage(store, boundary, tmp_path):
    path = tmp_path / 'pittsburgh-neighborhoods-wprdc.geojson'
    path.write_text('{"features":[]}')
    path.with_suffix('.source.json').write_text(json.dumps({'source': 'https://data.wprdc.org/dataset/neighborhoods2',
                                                           'publisher': 'City of Pittsburgh', 'sha256': 'bad', 'features': 90}))
    with pytest.raises(ValueError, match='provenance mismatch'):
        observed_coverage(store, boundary, [], path)
