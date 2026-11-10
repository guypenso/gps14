"""Fixed official Allegheny GIS query; no property-provider or personal data."""
import datetime as dt,hashlib,json,urllib.parse,urllib.request
from .request import NoRedirect

ENDPOINT='https://services1.arcgis.com/vdNDkVykv9vEWFX4/arcgis/rest/services/AlleghenyCountyMunicipalBoundaries/FeatureServer/0/query'
NAMES={'Pleasant Hills','Bethel Park'}

def retrieve(store,now):
    cached=store.execute('SELECT payload FROM settings WHERE name=?',('approved_municipal_evidence:v1',)).fetchone()
    if cached:return json.loads(cached[0])
    query=urllib.parse.urlencode({'where':"NAME IN ('Pleasant Hills','Bethel Park')",'outFields':'*','outSR':'4326','returnGeometry':'true','f':'geojson'})
    url=ENDPOINT+'?'+query
    with urllib.request.build_opener(NoRedirect).open(urllib.request.Request(url,headers={'Accept':'application/geo+json','User-Agent':'PENTEN-RE official municipal boundaries'}),timeout=20) as r:
        raw=r.read(1000001)
    if len(raw)>1000000:raise ValueError('Oversized boundary response')
    data=json.loads(raw)
    if data.get('error') or data.get('exceededTransferLimit') or len(data.get('features',[]))!=2:raise ValueError('Incomplete official municipality response')
    if {f['properties'].get('NAME') for f in data['features']}!=NAMES:raise ValueError('Unexpected municipalities')
    from shapely.geometry import shape,Point
    for f in data['features']:
        geometry=shape(f['geometry']);name=f['properties']['NAME']
        sample=(-80.038,40.325) if name=='Bethel Park' else (-79.962,40.333)
        if not geometry.is_valid or not geometry.covers(Point(*sample)):raise ValueError('Unexpected municipality geometry')
    result={'source':'https://data.wprdc.org/dataset/allegheny-county-municipal-boundaries','download_url':url,'retrieved_at':now.isoformat(),'sha256':hashlib.sha256(raw).hexdigest(),'geojson':data}
    store.save_setting('approved_municipal_evidence:v1',result)
    return result
