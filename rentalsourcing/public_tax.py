"""Official public CSV batch estimates. Never annual-bill verification or RentCast calls."""
import csv,datetime as dt,hashlib,io,json,re,tempfile,unicodedata,urllib.request
from decimal import Decimal,InvalidOperation,ROUND_HALF_UP
from pathlib import Path
from .request import NoRedirect

CSV_URL='https://data.wprdc.org/dataset/2b3df818-601e-4f06-b150-643557229491/resource/9a1c60bd-f9f7-4aba-aeb7-af8c3aaa44e5/download/data.csv'
SOURCE='https://data.wprdc.org/dataset/property-assessments'
WORDS={'street':'st','avenue':'ave','road':'rd','boulevard':'blvd','drive':'dr','lane':'ln','court':'ct','place':'pl','terrace':'ter','circle':'cir','highway':'hwy','north':'n','south':'s','east':'e','west':'w','apartment':'unit','apt':'unit','suite':'unit','ste':'unit'}


def norm(text):
    text=unicodedata.normalize('NFKC',str(text or '')).lower().replace('#',' unit ')
    return ' '.join(WORDS.get(w,w) for w in re.sub(r'[.,]',' ',text).split())


def municipality(value):
    text=norm(value)
    if text in {'bellevue','bellevue boro','bellevue borough'}:return 'Bellevue'
    if text=='pittsburgh' or ('ward' in text and text.endswith('pittsburgh')):return 'Pittsburgh'
    return None


def listing_key(p,boundary):
    muni='Bellevue' if boundary.bellevue.contains(p) else 'Pittsburgh'
    street=norm(p.get('addressLine1') or str(p.get('formattedAddress','')).split(',')[0])
    unit=norm(p.get('addressLine2'))
    if unit and not unit.startswith('unit '):unit='unit '+unit
    if unit and unit not in street:street+=' '+unit
    return street,str(p.get('zipCode','')).split('-')[0],muni


def parcel_key(row):
    muni=municipality(row.get('MUNIDESC'))
    number=str(row.get('PROPERTYHOUSENUM','')).strip();fraction=str(row.get('PROPERTYFRACTION','')).strip()
    street=norm(' '.join(x for x in [number,fraction,str(row.get('PROPERTYADDRESS','')).strip()] if x))
    unit=norm(row.get('PROPERTYUNIT'))
    if unit and not unit.startswith('unit '):unit='unit '+unit
    if unit:street+=' '+unit
    zipcode=str(row.get('PROPERTYZIP','')).strip().split('-')[0]
    if not number or number=='0' or not muni or not re.fullmatch(r'\d{5}',zipcode):return None
    return street,zipcode,muni


def unknown(now,warning,match='unmatched',sources=None,**extra):
    return dict(state='unknown',annual=None,year=2026,retrieved_at=now.isoformat(),match_status=match,warnings=[warning],sources=sources or [SOURCE],**extra)


def calculate(row,rates,now,source_sha):
    sources=[SOURCE]+rates['sources'];base=dict(parcel_id=row.get('PARID'),assessment_as_of=row.get('ASOFDATE'),source_sha256=source_sha)
    def fail(reason):return unknown(now,reason,'unique_parcel',sources,**base)
    try:
        if Decimal(str(row.get('TAXYEAR','')))!=rates['year'] or rates['year']!=now.year:return fail('Current certified assessment/rate year unavailable')
        if not re.fullmatch(r'[A-Z0-9]{16}',str(row.get('PARID','')).upper()):return fail('Official parcel identity unavailable')
        if str(row.get('TAXCODE','')).strip()!='T':return fail('Exempt/special tax structure requires bill verification')
        for key in ['HOMESTEADFLAG','FARMSTEADFLAG','ABATEMENTFLAG','CLEANGREEN']:
            if key not in row:return fail('Missing exemption/abatement field: '+key)
            value=str(row[key] or '').strip().upper()
            if value not in {'','N','NO','0','NONE'}:return fail('Recorded relief/abatement requires jurisdiction-specific bill review: '+key)
        muni=municipality(row.get('MUNIDESC'));r=rates['municipalities'].get(muni)
        if not r or norm(row.get('SCHOOLDESC'))!=norm(r['school']):return fail('Municipality/school district not uniquely validated')
        if not r.get('special_levies_complete'):return fail('Special levy scope not independently confirmed for municipality')
        if not row.get('ASOFDATE'):return fail('Assessment as-of date unavailable')
        county=Decimal(str(row.get('COUNTYTOTAL','')));local=Decimal(str(row.get('LOCALTOTAL','')))
        if not county.is_finite() or not local.is_finite() or min(county,local)<0:return fail('Taxable assessed values missing or invalid')
        inputs=[]
        for levy,value,mills in [('county',county,rates['county_mills']),('municipal',local,r['municipal_mills']),('school',local,r['school_mills']),('library',local,r['library_mills']),('parks',local,r['parks_mills'])]:
            rate=Decimal(mills)
            if rate<0 or not rate.is_finite():return fail('Invalid official rate')
            inputs.append(dict(levy=levy,taxable_assessment=float(value),mills=str(rate),amount=float(value*rate/1000)))
        annual=sum((Decimal(str(x['taxable_assessment']))*Decimal(x['mills'])/1000 for x in inputs),Decimal(0)).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)
    except (InvalidOperation,ValueError,TypeError,KeyError):return fail('Incomplete assessment/rate inputs')
    return dict(state='estimated',annual=float(annual),year=rates['year'],school_period=rates['school_period'],retrieved_at=now.isoformat(),match_status='unique_parcel',confidence='official_data_estimate_unverified_bill',assessment_flags={k:row[k] for k in ['HOMESTEADFLAG','FARMSTEADFLAG','ABATEMENTFLAG','CLEANGREEN']},sources=sources,inputs=inputs,formula='sum(taxable assessed value × applicable mills / 1000)',threshold_with_buffer=float(annual+100),below_threshold=annual+100<2000,warnings=['Estimate only; annual bill verification remains mandatory.','Published flag fields show no recorded relief; unrecorded owner-specific credits are not assumed or applied.','Current county/municipal year and current academic school-year rates are annualized together.','Future reassessment, sale, exemption or abatement changes may alter actual taxes.'],**base)


def match_csv(stream,candidates,boundary,rates,now,source_sha):
    keys={};matches={}
    for p in candidates:keys.setdefault(listing_key(p,boundary),[]).append(p['id'])
    csv.field_size_limit(1024*1024)
    reader=csv.DictReader(stream)
    needed={'PARID','PROPERTYHOUSENUM','PROPERTYFRACTION','PROPERTYADDRESS','PROPERTYUNIT','PROPERTYZIP','MUNIDESC','SCHOOLDESC','COUNTYTOTAL','LOCALTOTAL','TAXYEAR','ASOFDATE','TAXCODE','HOMESTEADFLAG','FARMSTEADFLAG','ABATEMENTFLAG','CLEANGREEN'}
    columns={k:re.sub('_','',k).upper() for k in reader.fieldnames or [] if k}
    columns={k:v for k,v in columns.items() if v in needed}
    if not {'PARID','PROPERTYHOUSENUM','PROPERTYADDRESS','MUNIDESC'}.issubset(set(columns.values())):raise ValueError('Unexpected public assessment schema')
    for original in reader:
        row={canonical:original.get(k) for k,canonical in columns.items()}
        key=parcel_key(row)
        if key not in keys:continue
        # Identical repeated rows are harmless; different parcels/inputs are ambiguous.
        relevant={k:row.get(k) for k in ['PARID','MUNIDESC','SCHOOLDESC','COUNTYTOTAL','LOCALTOTAL','TAXYEAR','ASOFDATE','TAXCODE','HOMESTEADFLAG','FARMSTEADFLAG','ABATEMENTFLAG','CLEANGREEN'] if k in row}
        matches.setdefault(key,{})[json.dumps(relevant,sort_keys=True)]=relevant
    result={}
    for key,ids in keys.items():
        rows=list(matches.get(key,{}).values())
        evidence=calculate(rows[0],rates,now,source_sha) if len(rows)==1 else unknown(now,'No unique official parcel/address match','ambiguous' if rows else 'unmatched',source_sha256=source_sha)
        for identity in ids:result[identity]=evidence
    return result


def download_and_match(candidates,boundary,now,opener=None):
    rates=json.loads((Path(__file__).resolve().parents[1]/'data/tax-rates-2026.json').read_text())
    opener=opener or urllib.request.build_opener(NoRedirect)
    sha=hashlib.sha256();size=0
    with tempfile.TemporaryFile() as file:
        with opener.open(urllib.request.Request(CSV_URL,headers={'Accept':'text/csv','User-Agent':'RentalSourcing public-data review'}),timeout=120) as response:
            if response.status!=200:raise ValueError('Public assessment download failed')
            while chunk:=response.read(65536):
                size+=len(chunk)
                if size>768*1024*1024:raise ValueError('Public assessment file exceeds safe limit')
                sha.update(chunk);file.write(chunk)
        file.seek(0)
        estimates=match_csv(io.TextIOWrapper(file,encoding='utf-8-sig',newline=''),candidates,boundary,rates,now,sha.hexdigest())
    return estimates,dict(source=SOURCE,download_url=CSV_URL,sha256=sha.hexdigest(),bytes=size,retrieved_at=now.isoformat(),year=rates['year'])


def import_local_csv(path,candidates,boundary,now):
    """Process one operator-supplied official CSV; still estimate-only evidence."""
    source=Path(path)
    if not source.is_file() or source.stat().st_size<=0 or source.stat().st_size>768*1024*1024:raise ValueError('Official assessment CSV missing or exceeds safe size')
    rates=json.loads((Path(__file__).resolve().parents[1]/'data/tax-rates-2026.json').read_text())
    sha=hashlib.sha256()
    with source.open('rb') as stream:
        for chunk in iter(lambda:stream.read(65536),b''):sha.update(chunk)
    with source.open('r',encoding='utf-8-sig',newline='') as stream:
        estimates=match_csv(stream,candidates,boundary,rates,now,sha.hexdigest())
    return estimates,dict(source=SOURCE,import_method='operator_supplied_official_csv',download_url=CSV_URL,sha256=sha.hexdigest(),bytes=source.stat().st_size,retrieved_at=now.isoformat(),year=rates['year'],official_bill_verified=False)
