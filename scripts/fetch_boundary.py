"""Fetch public official city geometry. Never contacts RentCast or Resend."""
import datetime as dt
import hashlib
import json
from pathlib import Path
import sys
import urllib.parse
import urllib.request
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from rentalsourcing.core import Boundary

BASE='https://services1.arcgis.com/YZCmUqbcsUpOKfj7/arcgis/rest/services/CityBoundary/FeatureServer/0'
SOURCE='https://data.wprdc.org/dataset/pittsburgh-city-boundary'

def install(destination='data/pittsburgh.geojson',opener=None):
    from shapely.geometry import shape,Point
    from shapely.ops import unary_union
    opener=opener or urllib.request.build_opener()
    url=BASE+'/query?'+urllib.parse.urlencode(dict(where='1=1',outFields='OBJECTID',outSR=4326,f='geojson',returnGeometry='true'))
    with opener.open(url,timeout=60) as r:
        raw=r.read(8*1024*1024)
    doc=json.loads(raw)
    if doc.get('exceededTransferLimit') or doc.get('type')!='FeatureCollection' or not doc.get('features'):
        raise ValueError('Incomplete city boundary response')
    geometry=unary_union([shape(f['geometry']) for f in doc['features']])
    if not geometry.is_valid or geometry.geom_type not in {'Polygon','MultiPolygon'}:raise ValueError('Invalid city polygon')
    if not geometry.contains(Point(-79.9961,40.4406)) or geometry.contains(Point(-80.0395,40.4965)):
        raise ValueError('City geometry failed Pittsburgh/Bellevue sanity check')
    path=Path(destination);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix('.tmp.geojson');temporary.write_bytes(raw)
    Boundary(temporary)
    temporary.replace(path)
    path.with_suffix('.source.json').write_text(json.dumps(dict(source=SOURCE,download_url=url,retrieved_at=dt.datetime.now(dt.timezone.utc).isoformat(),sha256=hashlib.sha256(raw).hexdigest()),indent=2))
    print('Verified city geometry saved; SHA256 '+hashlib.sha256(raw).hexdigest())

if __name__=='__main__':install(sys.argv[1] if len(sys.argv)>1 else 'data/pittsburgh.geojson')
