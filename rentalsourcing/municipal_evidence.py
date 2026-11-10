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
    failures=[];raw=None
    for endpoint in [ENDPOINT,'https://mapservices.pasda.psu.edu/server/rest/services/pasda/AlleghenyCounty/MapServer/21/query','https://gisdata.alleghenycounty.us/arcgis/rest/services/ACWebsite/RE_MapTab/MapServer/13/query']:
        url=endpoint+'?'+query
        try:
            with urllib.request.build_opener(NoRedirect).open(urllib.request.Request(url,headers={'Accept':'application/geo+json','User-Agent':'PENTEN-RE official municipal boundaries'}),timeout=12) as r:
                raw=r.read(1000001)
            if len(raw)>1000000:raise ValueError('Oversized boundary response')
            candidate=json.loads(raw)
            if candidate.get('error') or len(candidate.get('features',[]))!=2:raise ValueError('Incomplete source municipality response')
            break
        except Exception as exc:
            failures.append({'endpoint':endpoint,'error_type':type(exc).__name__,'http_status':getattr(exc,'code',None),'detail':str(exc)[:160] if isinstance(exc,ValueError) else None});raw=None
    if raw is None:return {'state':'unavailable','attempted_at':now.isoformat(),'failures':failures}
    data=json.loads(raw)
    if data.get('error') or data.get('exceededTransferLimit') or len(data.get('features',[]))!=2:raise ValueError('Incomplete official municipality response')
    warnings=[]
    if {str(f['properties'].get('NAME')).title() for f in data['features']}!=NAMES:warnings.append('Unexpected municipality labels')
    from shapely.geometry import shape,Point
    for f in data['features']:
        geometry=shape(f['geometry']);name=f['properties']['NAME'].title()
        sample=(-80.038,40.325) if name=='Bethel Park' else (-79.962,40.333)
        if not geometry.is_valid or not geometry.covers(Point(*sample)):warnings.append('Review geometry: '+str(name))
    result={'source':'https://data.wprdc.org/dataset/allegheny-county-municipal-boundaries','download_url':url,'retrieved_at':now.isoformat(),'sha256':hashlib.sha256(raw).hexdigest(),'geojson':data,'source_failures':failures,'validation_warnings':warnings}
    store.save_setting('approved_municipal_evidence:v1',result)
    return result
