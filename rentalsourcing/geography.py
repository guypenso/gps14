"""Production boundary loader requires the provenance produced by the official downloader."""
import hashlib
import json
from pathlib import Path
from .core import Boundary,number

SOURCE='https://data.wprdc.org/dataset/pittsburgh-city-boundary'
DOWNLOAD_PREFIX='https://services1.arcgis.com/YZCmUqbcsUpOKfj7/arcgis/rest/services/CityBoundary/FeatureServer/0/query?'

def load_city_boundary(path):
    path=Path(path)
    manifest=json.loads(path.with_suffix('.source.json').read_text())
    if manifest.get('source')!=SOURCE or not manifest.get('download_url','').startswith(DOWNLOAD_PREFIX):
        raise ValueError('Official boundary provenance missing')
    if hashlib.sha256(path.read_bytes()).hexdigest()!=manifest.get('sha256'):
        raise ValueError('City boundary checksum mismatch')
    return Boundary(path)

POLICY='pittsburgh-bellevue-brighton-spring-hill-sheraden-hazelwood-mount-washington-types-v4'
REGION_SOURCES={
 'bellevue':('https://data.wprdc.org/dataset/allegheny-county-municipal-boundaries','https://services1.arcgis.com/vdNDkVykv9vEWFX4/arcgis/rest/services/AlleghenyCountyMunicipalBoundaries/FeatureServer/0/query','NAME','Bellevue'),
 'brighton-heights':('https://pghbridgis.pittsburghpa.gov/federated/rest/services/Neighborhoods/FeatureServer/0','https://pghbridgis.pittsburghpa.gov/federated/rest/services/Neighborhoods/FeatureServer/0/query','hood','Brighton Heights'),
 'spring-hill-city-view':('https://pghbridgis.pittsburghpa.gov/federated/rest/services/Neighborhoods/FeatureServer/0','https://pghbridgis.pittsburghpa.gov/federated/rest/services/Neighborhoods/FeatureServer/0/query','hood','Spring Hill-City View'),
 'hazelwood':('https://pghbridgis.pittsburghpa.gov/federated/rest/services/Neighborhoods/FeatureServer/0','https://pghbridgis.pittsburghpa.gov/federated/rest/services/Neighborhoods/FeatureServer/0/query','hood','Hazelwood'),
 'mount-washington':('https://pghbridgis.pittsburghpa.gov/federated/rest/services/Neighborhoods/FeatureServer/0','https://pghbridgis.pittsburghpa.gov/federated/rest/services/Neighborhoods/FeatureServer/0/query','hood','Mount Washington'),
 'sheraden':('https://pghbridgis.pittsburghpa.gov/federated/rest/services/Neighborhoods/FeatureServer/0','https://pghbridgis.pittsburghpa.gov/federated/rest/services/Neighborhoods/FeatureServer/0/query','hood','Sheraden')}

def load_region(folder,slug):
    import urllib.parse
    path=Path(folder)/(slug+'.geojson');raw=path.read_bytes();manifest=json.loads(path.with_suffix('.source.json').read_text())
    source,endpoint,field,name=REGION_SOURCES[slug]
    url=manifest.get('download_url','');base,_,query=url.partition('?');q=urllib.parse.parse_qs(query)
    if (manifest.get('source')!=source or base!=endpoint or q.get('where')!=[f"{field}='{name}'"] or q.get('outSR')!=['4326'] or q.get('f')!=['geojson']):raise ValueError('Official region provenance missing')
    if hashlib.sha256(raw).hexdigest()!=manifest.get('sha256'):raise ValueError('Region boundary checksum mismatch')
    doc=json.loads(raw)
    if doc.get('exceededTransferLimit') or len(doc.get('features',[]))!=1 or str(next((v for k,v in doc['features'][0].get('properties',{}).items() if k.casefold()==field.casefold()),None)).strip().casefold()!=name.casefold():raise ValueError('Wrong/incomplete official region')
    return Boundary(path)

class EligibilityBoundary:
    def __init__(self,city,brighton,bellevue,sheraden,spring_hill,hazelwood=None,mount_washington=None):
        self.city=city;self.brighton=brighton;self.bellevue=bellevue;self.sheraden=sheraden;self.spring_hill=spring_hill
        self.hazelwood=hazelwood;self.mount_washington=mount_washington
        self.geometry=city.geometry.union(bellevue.geometry)
    def evaluate(self,row):
        from shapely.geometry import Point
        x,y=row.get('longitude'),row.get('latitude')
        if not number(x) or not number(y) or not -180<=x<=180 or not -90<=y<=90:return 'Pending Verification','coordinates'
        # covers also rejects exact boundary points; postal labels cannot override location.
        if self.sheraden.geometry.covers(Point(x,y)):return 'Rejected','sheraden_neighborhood'
        for region,reason in [(self.hazelwood,'hazelwood_neighborhood'),(self.mount_washington,'mount_washington_neighborhood')]:
            if region is None:return 'Pending Verification','neighborhood_boundaries_unavailable'
            if region.geometry.covers(Point(x,y)):return 'Rejected',reason
        if self.bellevue.contains(row):return 'Candidate',None
        if not self.city.contains(row):return 'Rejected','outside_city_or_boundary'
        if str(row.get('zipCode') or '').split('-')[0]=='15212' and not (self.brighton.contains(row) or self.spring_hill.contains(row)):return 'Rejected','outside_approved_neighborhoods_15212'
        return 'Candidate',None
    def contains(self,row):return self.evaluate(row)[0]=='Candidate'
    def search_parameters(self):
        import math
        west,south,east,north=self.geometry.bounds;lat=(south+north)/2;lon=(west+east)/2
        def miles(x,y):
            a,b=math.radians(lat),math.radians(y);dy=b-a;dx=math.radians(x-lon)
            return 3958.7613*2*math.asin(math.sqrt(math.sin(dy/2)**2+math.cos(a)*math.cos(b)*math.sin(dx/2)**2))
        radius=math.ceil((max(miles(x,y) for x in (west,east) for y in (south,north))+.01)*10)/10
        return dict(latitude=round(lat,7),longitude=round(lon,7),radius=radius)

def load_boundary(path):
    return EligibilityBoundary(load_city_boundary(path),load_region(Path(path).parent,'brighton-heights'),load_region(Path(path).parent,'bellevue'),load_region(Path(path).parent,'sheraden'),load_region(Path(path).parent,'spring-hill-city-view'),load_region(Path(path).parent,'hazelwood'),load_region(Path(path).parent,'mount-washington'))
