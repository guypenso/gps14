import datetime as dt
import json
import math
from pathlib import Path
from .provenance import demo_marked

EXCLUDED_ZIPS=frozenset({'15210','15203','15204'})


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def basic_rejection(row):
    if demo_marked(row): return 'demo_data'
    if row.get('status') != 'Active': return 'inactive'
    if row.get('state') != 'PA': return 'state'
    zipcode = str(row.get('zipCode', '')).split('-')[0]
    if len(zipcode) != 5 or not zipcode.isdigit(): return 'missing_zip'
    if zipcode in EXCLUDED_ZIPS: return 'excluded_zip'
    for field, low, high in [('bedrooms', 3, math.inf), ('bathrooms', 1, math.inf),
                              ('squareFootage', 1000, math.inf), ('price', 0.01, 200000)]:
        value = row.get(field)
        if not number(value) or not low <= value <= high: return field
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
    # Dollars compared in cents; strict inequality rejects $1,900 exactly.
    if round(total * 100) + 10000 >= 200000: return None
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


def verification(row, boundary):
    """Distinguish known failures from evidence that still requires verification."""
    if demo_marked(row): return 'Rejected', ['demo_data']
    missing=[]; failed=[]
    if str(row.get('propertyType') or '').strip().lower() in {'land','commercial','industrial'}:
        failed.append('non_residential')
    for field,expected in [('status','Active'),('state','PA')]:
        value=row.get(field)
        if value is None or value=='': missing.append(field)
        elif value!=expected: failed.append(field)
    zipcode=str(row.get('zipCode') or '').split('-')[0]
    if len(zipcode)!=5 or not zipcode.isdigit(): missing.append('zipCode')
    elif zipcode in EXCLUDED_ZIPS: failed.append('excluded_zip')
    for field,lo,hi in [('bedrooms',3,math.inf),('bathrooms',1,math.inf),('squareFootage',1000,math.inf),('price',0.01,200000)]:
        value=row.get(field)
        if not number(value): missing.append(field)
        elif not lo<=value<=hi: failed.append(field)
    if not isinstance(row.get('formattedAddress'),str) or not row['formattedAddress'].strip(): missing.append('address')
    if not isinstance(row.get('id'),str) or not row['id'].strip(): missing.append('property_id')
    x,y=row.get('longitude'),row.get('latitude')
    if not number(x) or not number(y) or not -180<=x<=180 or not -90<=y<=90:
        missing.append('coordinates')
    elif not boundary.contains(row): failed.append('outside_city_or_boundary')
    if failed: return 'Rejected',failed+missing
    if missing: return 'Pending Verification',missing
    return 'Candidate',[]


def tax_verification(record, today=None):
    from decimal import Decimal, InvalidOperation
    today=today or dt.date.today()
    taxes=record.get('propertyTaxes')
    if not isinstance(taxes,dict) or not taxes:
        return 'Pending Verification',None,'missing_tax_history'
    years=[int(k) for k in taxes if str(k).isdigit() and 1900<=int(k)<=today.year]
    if not years: return 'Pending Verification',None,'missing_tax_year'
    year=max(years);entry=taxes.get(str(year))
    if not isinstance(entry,dict) or entry.get('year')!=year or not number(entry.get('total')) or entry['total']<0:
        return 'Pending Verification',None,'invalid_latest_tax_amount'
    amount=Decimal(str(entry['total']));adjusted=amount+Decimal('100')
    tax={'year':year,'annual':float(amount),'adjusted':float(adjusted)}
    return ('Qualified',tax,None) if adjusted<Decimal('2000') else ('Rejected',tax,'annual_tax_too_high')


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
