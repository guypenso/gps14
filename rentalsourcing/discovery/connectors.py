"""Authorized RESO/CSV/public-record connectors. None is live until explicitly configured."""
from dataclasses import replace
import csv,io,json,os,re,time
import datetime as dt
from urllib.parse import urlsplit,urljoin,urlencode,parse_qs,unquote
import urllib.request
from ..request import NoRedirect
from .models import normalize,Source,Listing,numeric,instant
from .repository import ingest,register,health


def safe_endpoint(url,base):
    p=urlsplit(url);b=urlsplit(base)
    if p.scheme!='https' or p.hostname!=b.hostname or p.port!=b.port or p.username or p.password or p.fragment:
        raise PermissionError('Cross-origin/unsafe provider URL rejected')
    if unquote(p.path)!=b.path.rstrip('/')+'/Property':raise PermissionError('Provider URL escaped licensed resource')
    if any(k.lower() in {'access_token','api_key','token','key','password','client_secret'} for k in parse_qs(p.query)):raise PermissionError('Credentials in pagination URL rejected')
    return url


class ResoConnector:
    """RESO OData Property with bounded pages, OAuth/bearer, durable cursor and overlap.
    Endpoint/metadata/permissions must be approved; tests are fixtures, not licensed connectivity.
    """
    def __init__(self,source,base_url,*,token=None,token_url=None,client_id=None,client_secret=None,
                 max_pages=20,page_size=500,opener=None,expected_system=None):
        if source.kind!='mls' or not source.enabled:raise PermissionError('An enabled authorized MLS source is required')
        p=urlsplit(base_url)
        if p.scheme!='https' or not p.hostname or p.username or p.password or p.query or p.fragment:raise ValueError('Invalid MLS base URL')
        if not 1<=max_pages<=100 or not 1<=page_size<=1000:raise ValueError('Bounded pagination required')
        self.source=source;self.base=base_url.rstrip('/')+'/';self.token=token;self.token_url=token_url
        self.client_id=client_id;self.secret=client_secret;self.expiry=0;self.max_pages=max_pages;self.page_size=page_size
        self.opener=opener or urllib.request.build_opener(NoRedirect);self.expected_system=expected_system
    def _json(self,request):
        with self.opener.open(request,timeout=30) as response:
            raw=response.read(8_000_001)
            if response.status!=200 or len(raw)>8_000_000:raise ValueError('Provider response unavailable or too large')
            return json.loads(raw)
    def authenticate(self):
        if self.token and (not self.token_url or self.expiry>time.time()+30):return self.token
        if not self.token_url or not self.client_id or not self.secret:raise PermissionError('MLS credentials not configured securely')
        p=urlsplit(self.token_url)
        if p.scheme!='https' or p.hostname!=urlsplit(self.base).hostname or p.port!=urlsplit(self.base).port or p.username or p.password or p.fragment or p.query:raise PermissionError('Unsafe token endpoint')
        body=urlencode(dict(grant_type='client_credentials',scope='api',client_id=self.client_id,client_secret=self.secret)).encode()
        result=self._json(urllib.request.Request(self.token_url,data=body,headers={'Content-Type':'application/x-www-form-urlencoded'}))
        if not isinstance(result.get('access_token'),str) or not result['access_token']:raise PermissionError('MLS token unavailable')
        self.token=result['access_token'];self.expiry=time.time()+min(3600,int(result.get('expires_in',300)));return self.token
    def scan(self,store,now):
        register(store,self.source)
        state=store.execute('SELECT cursor FROM discovery_sources WHERE name=?',(self.source.name,)).fetchone()[0]
        checkpoint=json.loads(state) if state else {}
        # An interrupted scan resumes the exact same window/page. A completed one overlaps by5m.
        until=checkpoint.get('until') or now.isoformat()
        since=checkpoint.get('since') or ((instant(checkpoint['watermark'])-dt.timedelta(minutes=5)).isoformat() if checkpoint.get('watermark') else '1900-01-01T00:00:00+00:00')
        filters=f'ModificationTimestamp ge {since} and ModificationTimestamp le {until}'
        if self.expected_system:filters+=" and OriginatingSystemName eq '"+self.expected_system.replace("'","''")+"'"
        url=checkpoint.get('next') or self.base+'Property?'+urlencode({'$filter':filters,'$orderby':'ModificationTimestamp,ListingKey','$top':self.page_size})
        count=0;requests=0;seen=set()
        try:
            for _ in range(self.max_pages):
                safe_endpoint(url,self.base)
                if url in seen:raise ValueError('Provider pagination loop')
                seen.add(url)
                result=self._json(urllib.request.Request(url,headers={'Authorization':'Bearer '+self.authenticate(),'Accept':'application/json'}));requests+=1
                if not isinstance(result.get('value'),list):raise ValueError('Invalid RESO response')
                for row in result['value']:
                    if self.expected_system and row.get('OriginatingSystemName')!=self.expected_system:raise PermissionError('MLS scope/system mismatch')
                    listing=normalize(row,self.source,observed_at=now.isoformat())
                    ingest(store,listing,now,authorized=True);count+=1
                nxt=result.get('@odata.nextLink')
                if nxt:
                    url=safe_endpoint(urljoin(self.base,nxt),self.base)
                    cursor=json.dumps(dict(next=url,since=since,until=until))
                    # Page committed: persist cursor even if a later page fails. No false full success.
                    store.execute('UPDATE discovery_sources SET cursor=? WHERE name=?',(cursor,self.source.name))
                else:
                    health(store,self.source.name,now,success=True,cursor=json.dumps({'watermark':until}))
                    return dict(source=self.source.name,mode='licensed_api',complete=True,records=count,requests=requests,window_end=until)
            health(store,self.source.name,now,error='page_cap_reached')
            return dict(source=self.source.name,complete=False,records=count,requests=requests,error='page_cap_reached')
        except Exception as exc:
            health(store,self.source.name,now,error=type(exc).__name__)
            return dict(source=self.source.name,complete=False,records=count,requests=requests,error=type(exc).__name__)

    def verify_keys(self,store,keys,now):
        """Bounded current-status recheck of shortlisted stale records, not another full scan."""
        if not 1<=len(keys)<=20 or any(not isinstance(k,str) or len(k)>256 for k in keys):raise ValueError('Invalid point-verification batch')
        clause=' or '.join("ListingKey eq '"+k.replace("'","''")+"'" for k in keys)
        url=self.base+'Property?'+urlencode({'$filter':clause,'$top':len(keys)})
        result=self._json(urllib.request.Request(safe_endpoint(url,self.base),headers={'Authorization':'Bearer '+self.authenticate(),'Accept':'application/json'}))
        seen=set()
        for row in result.get('value',[]):
            if row.get('ListingKey') not in keys or (self.expected_system and row.get('OriginatingSystemName')!=self.expected_system):raise PermissionError('Point verification source identity mismatch')
            ingest(store,normalize(row,self.source,observed_at=now.isoformat()),now,authorized=True);seen.add(row['ListingKey'])
        return {'verified':len(seen),'unresolved':len(set(keys)-seen),'requests':1}


class CSVConnector:
    def __init__(self,source,column_map=None):
        self.source=source;self.column_map=column_map or {}
    def import_bytes(self,store,raw,exported_at,now,*,authorized=False):
        if not authorized:raise PermissionError('Approved broker export processing required')
        if len(raw)>10_000_000:raise ValueError('Export too large')
        instant(exported_at)
        if instant(exported_at)>now:raise ValueError('Future export')
        reader=csv.DictReader(io.StringIO(raw.decode('utf-8-sig')));count=0
        fields=reader.fieldnames or []
        if len(fields)!=len(set(fields)) or not {'ListingKey','ListingId'}&set(fields) or not {'UnparsedAddress','StreetAddress'}&set(fields):raise ValueError('Export contract headers missing/duplicated')
        for index,row in enumerate(reader):
            if index>=20000:raise ValueError('Export row cap reached')
            if None in row:raise ValueError('Malformed CSV column count')
            row={self.column_map.get(k,k):v for k,v in row.items()}
            ingest(store,normalize(row,self.source,observed_at=exported_at),now,authorized=True);count+=1
        register(store,self.source);health(store,self.source.name,now,success=True,cursor=json.dumps({'exported_at':exported_at}))
        store.execute('UPDATE discovery_sources SET last_success=? WHERE name=?',(exported_at,self.source.name))
        return dict(source=self.source.name,records=count,mode='authorized_export',complete=True)


class WPRDCConnector:
    """CC0 county characteristics only. Assessment/sale values are NEVER annual taxes/list prices."""
    RESOURCE='65855e14-549e-4992-b5be-d629afc676fa'
    def __init__(self,source,*,opener=None):
        if source.kind!='public_records' or not source.enabled:raise PermissionError('Approved public-record source required')
        self.source=source;self.opener=opener or urllib.request.build_opener(NoRedirect)
    def lookup(self,parcel,now):
        if not re.fullmatch(r'[A-Za-z0-9]{16}',parcel):raise ValueError('Exact county parcel ID required')
        query=urlencode(dict(resource_id=self.RESOURCE,filters=json.dumps({'PARID':parcel}),limit=2))
        req=urllib.request.Request('https://data.wprdc.org/api/3/action/datastore_search?'+query,headers={'Accept':'application/json'})
        with self.opener.open(req,timeout=20) as response:
            raw=response.read(1_000_001)
            if response.status!=200 or len(raw)>1_000_000:raise ValueError('Public-record response unavailable')
            data=json.loads(raw)
        rows=data.get('result',{}).get('records',[])
        if not data.get('success') or len(rows)!=1 or rows[0].get('PARID')!=parcel:raise ValueError('Missing/ambiguous public-record identity')
        return self.normalize(rows[0],now)
    def normalize(self,row,now):
        unit=(' Unit '+str(row['PROPERTYUNIT'])) if row.get('PROPERTYUNIT') else ''
        fraction=(' '+str(row['PROPERTYFRACTION'])) if row.get('PROPERTYFRACTION') else ''
        address=f"{row.get('PROPERTYHOUSENUM','')}{fraction} {row.get('PROPERTYADDRESS','')}{unit}, {row.get('PROPERTYCITY','')}, {row.get('PROPERTYSTATE','')}, {row.get('PROPERTYZIP','')}"
        from ..realscout import addresses
        parts=addresses(address)
        if len(parts)!=1:raise ValueError('Public record address requires review')
        a=parts[0]
        if a['state'].upper()!='PA':raise ValueError('Public parcel state mismatch')
        fields={'parcel_id':row['PARID'],'county':'Allegheny','state':'PA','zipCode':a['zip'][:5]}
        for old,new in [('BEDROOMS','bedrooms'),('FINISHEDLIVINGAREA','squareFootage')]:
            if row.get(old) is not None:fields[new]=numeric(row[old])
        if row.get('FULLBATHS') is not None and row.get('HALFBATHS') is not None:fields['bathrooms']=numeric(row['FULLBATHS'])+numeric(row['HALFBATHS'])/2
        if row.get('USEDESC'):fields['propertyType']=row['USEDESC']
        date=str(row.get('ASOFDATE') or '')
        if not date:raise ValueError('Public dataset observation date required')
        stamp=date+'T00:00:00+00:00' if len(date)==10 else date
        return Listing(self.source,str(row['PARID']),a['street'],a['city'],'PA',a['zip'][:5],stamp,fields)
