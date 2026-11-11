import datetime as dt
import json
import math
import re
from pathlib import Path
from .provenance import demo_marked
from .turnkey import evidence as turnkey_evidence
from .condominium import evidence as condominium_evidence
from .investment_policy import PRICE_MAX, TAX_BUFFER, TAX_CEILING, annual_tax_passes,bedroom_price_rejection

EXCLUDED_ZIPS=frozenset({'15210','15203','15204','15211'})


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def property_type_verification(row, *, single_family=False):
    """Structured use/attachment evidence only; architectural style is not a property type."""
    condo=condominium_evidence(row)
    if condo['state']=='excluded':return 'Rejected',condo['reason']
    fields=('propertyType','propertySubType','propertySubtype','structureType','dwellingType')
    values=[re.sub(r'[^a-z0-9]+','',str(row[k]).lower()) for k in fields if row.get(k)]
    town={'townhouse','townhome','townhouses','townhomes','attachedtownhouse','attachedtownhome','rowhouse','rowhome','rowhouses','rowhomes','singlefamilyattached','attachedsinglefamily','singlefamilyresidenceattached','terracedhouse','terracedhome','terracehouse','attachedrowhouse'}
    if any(v in town or v.startswith(('townhouse','townhome')) or v.endswith(('townhouse','townhome','rowhouse','rowhome','terracedhouse')) for v in values):return 'Rejected','townhouse'
    if any(v in {'land','commercial','industrial'} for v in values):return 'Rejected','non_residential'
    if condo['state']=='pending':return 'Pending Verification',condo['reason']
    if single_family and any(v in {'multifamily','duplex','triplex','fourplex','residentialincome','manufactured','manufacturedhome','mobilehome','commercial','land'} for v in values):return 'Rejected','single_family_only'
    residential={'singlefamily','singlefamilyresidence','singlefamilydetached','detachedsinglefamily','singlefamilyhome','detached','multifamily','duplex','triplex','fourplex','manufactured','manufacturedhome','mobilehome','residentialincome','singlefamilyresidential','singlefamilyresidencedetached','detachedsinglefamilyresidence','sfh','sfr'}
    if row.get('propertyType') and re.sub(r'[^a-z0-9]+','',str(row['propertyType']).lower()) in residential:
        if row.get('attached') is True or row.get('isAttached') is True or any(v in {'attached','semidetached','singlefamilysemidetached','semidetachedsinglefamily','duet','twinhome'} for v in values):
            return 'Pending Verification','attached_property_type_unverified'
        return 'Candidate',None
    return 'Pending Verification','property_type_unverified'


def geographic_verification(row,boundary):
    if hasattr(boundary,'evaluate'):return boundary.evaluate(row)
    if not boundary.contains(row):return 'Rejected','outside_city_or_boundary'
    if str(row.get('zipCode') or '').split('-')[0]=='15212':return 'Pending Verification','brighton_heights_boundary_unavailable'
    return 'Pending Verification','neighborhood_boundaries_unavailable'


def report_eligible(row,boundary):
    """Pending tax/type evidence is allowed; geographic inclusion must be proved."""
    return verification(row,boundary)[0]!='Rejected' and geographic_verification(row,boundary)[0]=='Candidate'


def basic_rejection(row):
    if demo_marked(row): return 'demo_data'
    if turnkey_evidence(row)['classified']:return 'turnkey'
    if row.get('status') != 'Active': return 'inactive'
    if row.get('state') != 'PA': return 'state'
    zipcode = str(row.get('zipCode', '')).split('-')[0]
    if len(zipcode) != 5 or not zipcode.isdigit(): return 'missing_zip'
    if zipcode in EXCLUDED_ZIPS: return 'excluded_zip'
    if property_type_verification(row,single_family=True)[0]=='Rejected':return property_type_verification(row,single_family=True)[1]
    bedroom_failure=bedroom_price_rejection(row.get('bedrooms'),row.get('price') if number(row.get('price')) else None)
    if bedroom_failure:return bedroom_failure
    for field, low, high in [('bedrooms', 2, 4), ('bathrooms', 1, math.inf),
                              ('squareFootage', 1000, math.inf), ('price', 0.01, PRICE_MAX)]:
        value = row.get(field)
        if not number(value) or not low <= value <= high or field=='bedrooms' and not float(value).is_integer(): return field
    if not row.get('id') or not row.get('formattedAddress'): return 'identity'
    return None


def latest_tax(record, today=None):
    """Latest reported annual total; never substitute assessed value or an older valid total."""
    today = today or dt.date.today()
    taxes = record.get('propertyTaxes')
    if not isinstance(taxes, dict) or not taxes: return None
    years = [int(k) for k in taxes if str(k).isdigit() and 1900 <= int(k) <= today.year]
    if not years: return None
    year = max(years)
    entry = taxes.get(str(year))
    if not isinstance(entry, dict) or entry.get('year') != year: return None
    total = entry.get('total')
    if not number(total) or total < 0: return None
    if not annual_tax_passes(total): return None
    return {'year': year, 'annual': total, 'adjusted': round(total + 100, 2)}


class Boundary:
    """Offline GeoJSON WGS84 Polygon/MultiPolygon membership, including holes."""
    def __init__(self, path):
        doc = json.loads(Path(path).read_text())
        crs = doc.get('crs', {}).get('properties', {}).get('name', '')
        if crs and not any(s in crs for s in ['4326', 'CRS84']):
            raise ValueError('Boundary must use WGS84 longitude/latitude')
        features = doc.get('features', []) if doc.get('type') == 'FeatureCollection' else [doc]
        self.polygons = []
        for f in features:
            g = f.get('geometry') if f.get('type') == 'Feature' else f
            if not g or g.get('type') not in {'Polygon', 'MultiPolygon'}:
                raise ValueError('Polygon boundary required')
            polygons = [g['coordinates']] if g['type'] == 'Polygon' else g['coordinates']
            for polygon in polygons:
                if not polygon: raise ValueError('Empty polygon')
                for ring in polygon:
                    if len(ring) < 4 or ring[0] != ring[-1]: raise ValueError('Unclosed ring')
                    if any(len(p) < 2 or not number(p[0]) or not number(p[1]) or
                           not -81 < p[0] < -79 or not 39 < p[1] < 42 for p in ring):
                        raise ValueError('Boundary coordinates outside Pittsburgh region')
                self.polygons.append(polygon)
        if not self.polygons: raise ValueError('Empty boundary')
        from shapely.geometry import Polygon
        from shapely.ops import unary_union
        self.geometry=unary_union([Polygon(p[0],p[1:]) for p in self.polygons])
        if not self.geometry.is_valid: raise ValueError('Invalid city boundary geometry')

    @staticmethod
    def ring(x, y, ring):
        inside = False
        for a, b in zip(ring, ring[1:]):
            ax, ay = a[:2]; bx, by = b[:2]
            cross = (x-ax)*(by-ay) - (y-ay)*(bx-ax)
            if abs(cross) < 1e-12 and min(ax,bx) <= x <= max(ax,bx) and min(ay,by) <= y <= max(ay,by):
                return None  # Border cannot be confirmed; exclude conservatively.
            if (ay > y) != (by > y) and x < (bx-ax)*(y-ay)/(by-ay)+ax:
                inside = not inside
        return inside

    def contains(self, row):
        x, y = row.get('longitude'), row.get('latitude')
        if not number(x) or not number(y): return False
        from shapely.geometry import Point
        return bool(self.geometry.contains(Point(x,y)))


def verification(row, boundary, *, profile="rental", offering=True):
    """Distinguish known failures from evidence that still requires verification."""
    if demo_marked(row): return 'Rejected', ['demo_data']
    missing=[]; failed=[]
    if turnkey_evidence(row)['classified']:failed.append('turnkey')
    if profile not in ('rental','offmarket'):raise ValueError('Unknown eligibility profile')
    type_state,type_reason=property_type_verification(row,single_family=True)
    if type_state=='Rejected':failed.append(type_reason)
    elif type_state=='Pending Verification':missing.append(type_reason)
    for field,expected in ([('status','Active')] if offering else [])+[('state','PA')]:
        value=row.get(field)
        if value is None or value=='': missing.append(field)
        elif value!=expected: failed.append(field)
    zipcode=str(row.get('zipCode') or '').split('-')[0]
    if len(zipcode)!=5 or not zipcode.isdigit(): missing.append('zipCode')
    elif zipcode in EXCLUDED_ZIPS: failed.append('excluded_zip')
    limits=[('bedrooms',2,4),('bathrooms',1,math.inf),('squareFootage',1000,math.inf)]
    if offering:limits.append(('price',30000 if profile=='offmarket' else 0.01,PRICE_MAX))
    for field,lo,hi in limits:
        value=row.get(field)
        if not number(value): missing.append(field)
        elif not lo<=value<=hi or field=='bedrooms' and not float(value).is_integer(): failed.append(field)
    bedroom_failure=bedroom_price_rejection(row.get('bedrooms'),row.get('price') if offering and number(row.get('price')) else None)
    if bedroom_failure and number(row.get('bedrooms')) and bedroom_failure not in failed:failed.append(bedroom_failure)
    if not isinstance(row.get('formattedAddress'),str) or not row['formattedAddress'].strip(): missing.append('address')
    if not isinstance(row.get('id'),str) or not row['id'].strip(): missing.append('property_id')
    x,y=row.get('longitude'),row.get('latitude')
    if not number(x) or not number(y) or not -180<=x<=180 or not -90<=y<=90:
        missing.append('coordinates')
    else:
        geo_state,geo_reason=geographic_verification(row,boundary)
        if geo_state=='Rejected':failed.append(geo_reason)
        elif geo_state=='Pending Verification':missing.append(geo_reason)
    if failed: return 'Rejected',failed+missing
    if missing: return 'Pending Verification',missing
    return 'Candidate',[]


def tax_verification(record, today=None):
    from decimal import Decimal, InvalidOperation
    today=today or dt.date.today()
    type_state,type_reason=property_type_verification(record)
    type_evidence=any(record.get(k) for k in ('propertyType','propertySubType','propertySubtype','structureType','dwellingType','attached','isAttached','ownershipType','ownership','commonInterest','isCondominium','isCondo')) or condominium_evidence(record)['state']!='clear'
    if type_evidence and type_state=='Rejected':return 'Rejected',None,type_reason
    taxes=record.get('propertyTaxes')
    if not isinstance(taxes,dict) or not taxes:
        return 'Pending Verification',None,'missing_tax_history'
    years=[int(k) for k in taxes if str(k).isdigit() and 1900<=int(k)<=today.year]
    if not years: return 'Pending Verification',None,'missing_tax_year'
    year=max(years);entry=taxes.get(str(year))
    if not isinstance(entry,dict) or entry.get('year')!=year or not number(entry.get('total')) or entry['total']<0:
        return 'Pending Verification',None,'invalid_latest_tax_amount'
    amount=Decimal(str(entry['total']));adjusted=amount+TAX_BUFFER
    tax={'year':year,'annual':float(amount),'adjusted':float(adjusted)}
    if adjusted>TAX_CEILING:
        rent=record.get('_rentcast_rent') or {}
        try:rent_date=dt.datetime.fromisoformat(rent.get('observed_at','').replace('Z','+00:00')).date()
        except (ValueError,TypeError):rent_date=None
        exception=rent.get('source')=='RentCast' and rent.get('method')=='long_term_avm' and rent.get('source_id')==record.get('id') and number(rent.get('monthly')) and rent['monthly']>2300 and rent_date is not None and 0<=(today-rent_date).days<=30 and len(rent.get('comparables',[]))>=3 and year>=today.year-1
        if not exception:return 'Rejected',tax,'annual_tax_too_high'
        tax['eligibility_path']='high_rent_exception'
    if type_evidence and type_state=='Pending Verification':return 'Pending Verification',tax,type_reason
    return 'Qualified',tax,None


def historical_reduction(row):
    """Only compare dated price observations within the current listing period."""
    current=row.get('price')
    if not number(current): return None
    listed=str(row.get('listedDate') or '')[:10]
    history=row.get('history')
    if not listed or not isinstance(history,dict): return None
    earlier=[]
    for date,entry in history.items():
        if isinstance(entry,dict) and listed<=str(date)[:10] and number(entry.get('price')):
            earlier.append((str(date),entry['price']))
    earlier.sort()
    if earlier and earlier[-1][1]>current: return earlier[-1][1]
    return None
