"""Pinned City/WPRDC geometry; never approximate the user's hand-drawn edge.

The City download is currently inaccessible from the execution environment.
This published export is traceable to WPRDC and matches eight separately
downloaded City polygons within floating-point precision (see source manifest).
"""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from shapely.geometry import shape

SNAPSHOT_SHA256 = '4714bc178ec948babd2bd989dc158f99fb359207636c5de730134322a6d41bd5'
NAMES = {
    'homewood-north': 'Homewood North',
    'homewood-south': 'Homewood South',
    'homewood-west': 'Homewood West',
    'east-hills': 'East Hills',
    'lincoln-lemington-belmar-south': 'Lincoln-Lemington-Belmar',
}


def load_pink_regions(folder):
    path = Path(folder) / 'pittsburgh-neighborhoods-wprdc.geojson'
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != SNAPSHOT_SHA256:
        raise ValueError('WPRDC neighborhood snapshot checksum mismatch')
    doc = json.loads(raw)
    if doc.get('type') != 'FeatureCollection' or len(doc.get('features', [])) != 90:
        raise ValueError('Incomplete WPRDC neighborhood snapshot')
    result = {}
    for slug, name in NAMES.items():
        matches = [f for f in doc['features'] if f['properties'].get('hood') == name]
        if len(matches) != 1:
            raise ValueError('Ambiguous WPRDC neighborhood identity')
        geometry = shape(matches[0]['geometry'])
        if slug == 'lincoln-lemington-belmar-south':
            # Select an existing complete component, never clip at guessed latitude.
            if geometry.geom_type != 'MultiPolygon' or len(geometry.geoms) != 2:
                raise ValueError('Lincoln-Lemington-Belmar components changed')
            geometry = geometry.geoms[0]
        if not geometry.is_valid or geometry.is_empty:
            raise ValueError('Invalid WPRDC neighborhood geometry')
        result[slug] = SimpleNamespace(geometry=geometry)
    return result
