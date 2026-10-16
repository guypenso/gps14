"""Production boundary loader requires the provenance produced by the official downloader."""
import hashlib
import json
from pathlib import Path
from .core import Boundary

SOURCE='https://data.wprdc.org/dataset/pittsburgh-city-boundary'
DOWNLOAD_PREFIX='https://services1.arcgis.com/YZCmUqbcsUpOKfj7/arcgis/rest/services/CityBoundary/FeatureServer/0/query?'

def load_boundary(path):
    path=Path(path)
    manifest=json.loads(path.with_suffix('.source.json').read_text())
    if manifest.get('source')!=SOURCE or not manifest.get('download_url','').startswith(DOWNLOAD_PREFIX):
        raise ValueError('Official boundary provenance missing')
    if hashlib.sha256(path.read_bytes()).hexdigest()!=manifest.get('sha256'):
        raise ValueError('City boundary checksum mismatch')
    return Boundary(path)
