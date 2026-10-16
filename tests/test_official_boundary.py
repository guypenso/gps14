"""Verify the bundled official geometry offline, including provenance integrity."""
import json
from pathlib import Path
import pytest
from rentalsourcing.geography import load_boundary

ROOT = Path(__file__).resolve().parents[1]


def test_official_city_contains_downtown_but_excludes_neighboring_municipalities():
    boundary = load_boundary(ROOT / 'data/pittsburgh.geojson')
    for longitude, latitude in [(-79.9961, 40.4406), (-79.953, 40.444)]:
        assert boundary.contains(dict(longitude=longitude, latitude=latitude))
    for longitude, latitude in [(-80.0395, 40.4965), (-80.066, 40.39), (-79.886, 40.442)]:
        assert not boundary.contains(dict(longitude=longitude, latitude=latitude))
    assert not boundary.contains(dict(longitude=None, latitude=40.44))
    point = list(boundary.geometry.exterior.coords)[0] if boundary.geometry.geom_type == 'Polygon' else list(boundary.geometry.geoms[0].exterior.coords)[0]
    assert not boundary.contains(dict(longitude=point[0], latitude=point[1]))


def test_official_city_tampering_fails_closed(tmp_path):
    original = ROOT / 'data/pittsburgh.geojson'
    target = tmp_path / original.name
    target.write_bytes(original.read_bytes() + b' ')
    target.with_suffix('.source.json').write_bytes(original.with_suffix('.source.json').read_bytes())
    with pytest.raises(ValueError, match='checksum'):
        load_boundary(target)
