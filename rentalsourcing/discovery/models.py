"""Provider-independent, permission-bound listing/evidence contract."""
from dataclasses import dataclass,field,asdict
import datetime as dt
import hashlib,json,math,re
from urllib.parse import urlsplit
from ..realscout import addresses,address_key

KINDS={'mls','realscout','rentcast','public_records'}
FIELDS={'price','bedrooms','bathrooms','squareFootage','status','state','zipCode','latitude','longitude',
        'propertyType','parcel_id','county','annual_tax','tax_year','monthly_rent','rent_low','rent_high',
        'estimated_value','hoa_monthly','days_on_market','unit_count'}
MARKET={'price','status','days_on_market'}


def instant(value):
    if not isinstance(value,str):raise ValueError('Timezone-aware timestamp required')
    v=dt.datetime.fromisoformat(value.replace('Z','+00:00'))
    if v.tzinfo is None:raise ValueError('Timezone-aware timestamp required')
    return v.astimezone(dt.timezone.utc)


def numeric(value,*,signed=False):
    if value in (None,''):return None
    if isinstance(value,bool):raise ValueError('Boolean is not a property measurement')
    result=float(str(value).replace(',','').replace('$','').strip())
    if not math.isfinite(result) or (result<0 and not signed):raise ValueError('Invalid nonnegative measurement')
    return result


def public_url(value):
    if not value:return None
    p=urlsplit(value)
    if p.scheme!='https' or not p.hostname or p.username or p.password or p.fragment or p.query:
        raise ValueError('Only non-tokenized HTTPS listing URLs may enter the unified store')
    if '/private/' in p.path or '/c/' in p.path:raise ValueError('Use encrypted RealScout link gateway')
    return value


@dataclass(frozen=True)
class Source:
    name:str
    kind:str
    permission_id:str
    scope:str
    enabled:bool=False
    authoritative:bool=False
    synthetic:bool=False
    def __post_init__(self):
        if not re.fullmatch(r'[a-z0-9_-]{1,60}',self.name) or self.kind not in KINDS:raise ValueError('Invalid source')
        if not self.permission_id or not self.scope:raise PermissionError('Explicit permitted use and scope required')
        if self.kind not in {'mls','public_records'} and self.authoritative:raise ValueError('Authority cannot be asserted by an alert/aggregator')


@dataclass(frozen=True)
class Listing:
    source:Source
    record_id:str
    street:str
    city:str
    state:str
    zipcode:str
    observed_at:str
    fields:dict
    mls_id:str|None=None
    mls_system:str|None=None
    url:str|None=None
    estimates:tuple=()
    deleted:bool=False
    modified_at:str|None=None
    comparables:tuple=()
    def __post_init__(self):
        if not isinstance(self.record_id,str) or not self.record_id or len(self.record_id)>256:raise ValueError('Source record identity required')
        instant(self.observed_at)
        if self.modified_at and instant(self.modified_at)>instant(self.observed_at):raise ValueError('Future modification timestamp')
        if not self.street or not self.city or not re.fullmatch(r'\d{5}',self.zipcode):raise ValueError('Unambiguous full address required')
        if set(self.fields)-FIELDS or not set(self.estimates)<=set(self.fields):raise ValueError('Unsupported evidence field')
        if self.source.kind=='public_records' and set(self.fields)&MARKET:raise ValueError('Public assessments do not establish active listing status/price')
        for k,v in self.fields.items():
            if k not in {'status','state','zipCode','propertyType','parcel_id','county'} and v is not None:numeric(v,signed=k in {'latitude','longitude'})
        public_url(self.url)
    @property
    def key(self):return address_key(self.street,self.city,self.state,self.zipcode)
    @property
    def entity_id(self):return 'property:'+hashlib.sha256(self.key.encode()).hexdigest()
    def payload(self):
        d=asdict(self);d['source']=asdict(self.source);return d


class OutOfScopeListing(ValueError):
    """Recognized non-purchase record, not a malformed purchase candidate."""


def normalize(row,source,*,observed_at=None,style='reso'):
    if not isinstance(row,dict):raise ValueError('Listing must be an object')
    from ..provenance import demo_marked
    if demo_marked(row) and not source.synthetic:raise PermissionError('Demo data cannot become production evidence')
    if str(row.get('PropertyType','')).replace(' ','').lower() in {'residentiallease','commerciallease','rental'}:
        raise OutOfScopeListing('Rental listings are not purchase listings')
    mapping={'ListPrice':'price','BedroomsTotal':'bedrooms','BathroomsTotalInteger':'bathrooms',
      'LivingArea':'squareFootage','StandardStatus':'status','PropertySubType':'propertyType',
      'Latitude':'latitude','Longitude':'longitude','TaxAnnualAmount':'annual_tax','TaxYear':'tax_year',
      'NumberOfUnitsTotal':'unit_count','EstimatedMonthlyRent':'monthly_rent','EstimatedValue':'estimated_value','DaysOnMarket':'days_on_market','ParcelNumber':'parcel_id','CountyOrParish':'county'}
    if style=='rentcast':
        mapping={k:k for k in ['price','bedrooms','bathrooms','squareFootage','status','propertyType','latitude','longitude']}
        address=row.get('formattedAddress','');identity=row.get('id');stamp=observed_at
    else:
        address=row.get('UnparsedAddress') or ', '.join(filter(None,[row.get('StreetAddress'),row.get('City'),row.get('StateOrProvince'),str(row.get('PostalCode') or '')]))
        identity=row.get('ListingKey') or row.get('ListingId');stamp=observed_at or row.get('ModificationTimestamp')
    found=addresses(address)
    if not found and style=='reso':
        address=', '.join(filter(None,[address,row.get('City'),row.get('StateOrProvince'),str(row.get('PostalCode') or '')]));found=addresses(address)
    if len(found)!=1:raise ValueError('Address requires review; fuzzy matching is not authorized')
    a=found[0];state='PA' if a['state'].lower()=='pennsylvania' else a['state'].upper();fields={v:row[k] for k,v in mapping.items() if row.get(k) not in (None,'')}
    fields.update(state=state,zipCode=a['zip'][:5])
    numeric_names=FIELDS-{'status','state','zipCode','propertyType','parcel_id','county'}
    for k in set(fields)&numeric_names:fields[k]=numeric(fields[k],signed=k in {'latitude','longitude'})
    if row.get('BathroomsFull') is not None and row.get('BathroomsHalf') is not None:
        fields['bathrooms']=numeric(row['BathroomsFull'])+numeric(row['BathroomsHalf'])/2
    if 'propertyType' not in fields and row.get('PropertyType'):fields['propertyType']=row['PropertyType']
    if 'status' in fields:
        fields['status']={'Closed':'Sold','ActiveUnderContract':'Pending','Active Under Contract':'Pending','ComingSoon':'Coming Soon'}.get(fields['status'],fields['status'])
    amount=row.get('AssociationFee');period=row.get('AssociationFeeFrequency')
    if amount not in (None,'') and period in {'Monthly','Annually','Quarterly'}:
        fields['hoa_monthly']=numeric(amount)/{'Monthly':1,'Annually':12,'Quarterly':3}[period]
    return Listing(source,str(identity or ''),a['street'],a['city'],state,a['zip'][:5],stamp,fields,
        str(row['ListingId']) if row.get('ListingId') else None,row.get('OriginatingSystemName'),
        row.get('ListingURL') or row.get('PublicRemarksURL') or row.get('listingUrl'),tuple(k for k in ('monthly_rent','estimated_value') if k in fields),bool(row.get('DeletedYN',False)),row.get('ModificationTimestamp') if style=='reso' else None)
