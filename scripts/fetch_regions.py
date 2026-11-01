"""Download public county/city polygons only. Never contacts a property or email provider."""
import datetime as dt
import hashlib
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from rentalsourcing.core import Boundary

REGIONS={
 'bellevue':dict(source='https://data.wprdc.org/dataset/allegheny-county-municipal-boundaries',
  endpoint='https://services1.arcgis.com/vdNDkVykv9vEWFX4/arcgis/rest/services/AlleghenyCountyMunicipalBoundaries/FeatureServer/0/query',
  field='NAME',name='Bellevue'),
 'brighton-heights':dict(source='https://pghbridgis.pittsburghpa.gov/federated/rest/services/Neighborhoods/FeatureServer/0',
  endpoint='https://pghbridgis.pittsburghpa.gov/federated/rest/services/Neighborhoods/FeatureServer/0/query',
  field='hood',name='Brighton Heights'),
 'spring-hill-city-view':dict(source='https://pghbridgis.pittsburghpa.gov/federated/rest/services/Neighborhoods/FeatureServer/0',
  endpoint='https://pghbridgis.pittsburghpa.gov/federated/rest/services/Neighborhoods/FeatureServer/0/query',
  field='hood',name='Spring Hill-City View'),
 'sheraden':dict(source='https://pghbridgis.pittsburghpa.gov/federated/rest/services/Neighborhoods/FeatureServer/0',
  endpoint='https://pghbridgis.pittsburghpa.gov/federated/rest/services/Neighborhoods/FeatureServer/0/query',
  field='hood',name='Sheraden')}

def install(folder='data',opener=None,regions=None):
 opener=opener or urllib.request.build_opener()
 for slug in regions or REGIONS:
  source=REGIONS[slug]
  params=dict(where=f"{source['field']}='{source['name']}'",outFields=source['field'],outSR=4326,returnGeometry='true',f='geojson')
  url=source['endpoint']+'?'+urllib.parse.urlencode(params)
  with opener.open(url,timeout=60) as response:raw=response.read(8*1024*1024+1)
  if len(raw)>8*1024*1024:raise ValueError('Oversized polygon response')
  doc=json.loads(raw)
  if doc.get('exceededTransferLimit') or doc.get('type')!='FeatureCollection' or len(doc.get('features',[]))!=1:raise ValueError('Incomplete/ambiguous region')
  properties=doc['features'][0].get('properties',{})
  label=next((v for k,v in properties.items() if k.casefold()==source['field'].casefold()),None)
  if str(label).strip().casefold()!=source['name'].casefold():raise ValueError('Wrong region returned: '+repr(properties))
  print(slug,'official label:',repr(label))
  path=Path(folder)/f'{slug}.geojson';path.parent.mkdir(parents=True,exist_ok=True)
  temporary=path.with_suffix('.tmp.geojson');temporary.write_bytes(raw);boundary=Boundary(temporary)
  if not 0.00002<boundary.geometry.area<0.003:raise ValueError('Unexpected region area')
  temporary.replace(path)
  path.with_suffix('.source.json').write_text(json.dumps(dict(**source,download_url=url,retrieved_at=dt.datetime.now(dt.timezone.utc).isoformat(),sha256=hashlib.sha256(raw).hexdigest()),indent=2)+'\n')
  print(slug,'verified:',len(raw),'bytes; SHA256',hashlib.sha256(raw).hexdigest())
if __name__=='__main__':install(sys.argv[1] if len(sys.argv)>1 else 'data',regions=sys.argv[2:] or None)
