"""One-time accepted-email backfill. Read-only PostgreSQL; signed receiver writes only.

No scanners, email sender, migrations, source updates or schedule/config mutations.
Default is dry-run. --execute requires the exact verified report hashes below.
No production records or credentials are embedded in this source file.
"""
import argparse,datetime,getpass,gzip,hashlib,hmac,html,json,os,pathlib,re,sys,time,urllib.request,urllib.error,warnings,zlib
sys.path.insert(0,str(pathlib.Path.cwd()))
import rentalsourcing.digest as digest
from rentalsourcing.request import NoRedirect
URL='https://inspection-command-windows-20261017.roundmesa2.chatgpt.site/api/rentalsourcing/sync'
DAYS=('2026-10-27','2026-10-28')
EXPECTED_REPORT_SHA256={'2026-10-27': '66095d61ecf6e4468ac1512263a9770ff0b14d2ea239b19e5412d21b40923792', '2026-10-28': '4f6b219d3fcdf0197022f8bdfae4809eb0715d7b2a933036e427babffe7e7a24'}

class BackfillValidationError(RuntimeError):pass

EXCLUDED={'15203','15204','15210','15211'}
pattern=re.compile(r'^(?P<address>.*?) \| \$(?P<price>.*?) \| (?P<beds>.*?) beds / (?P<baths>.*?) baths / (?P<area>.*?) sqft \| ZIP (?P<zip>.*?) \| Status (?P<status>.*?)\nTax/year: (?P<tax>.*?) \| Missing: (?P<reasons>.*?)\nObserved: (?P<observed>.*?) \| Source: RentCast\nListing: (?P<link>.*)$',re.S)
def number(v):
 if v in ('Missing','None',''):return None
 n=float(v.replace(',',''));assert n>=0;return int(n) if n.is_integer() else n
def reconstruct(evidence):
 events={x['id']:x for x in evidence['selected_events']}
 reports=[];summary=[]
 for r in evidence['reports']:
  assert r['state']=='sent' and r['provider_id'] and r['first_attempt_at'],'No accepted daily report evidence'
  day=r['job_id'].split(':',1)[1];assert day in ('2026-10-27','2026-10-28');assert r['id']=='daily-'+hashlib.sha256(r['job_id'].encode()).hexdigest()
  body=json.loads(r['payload']);text=body['text'];assert text.startswith('RentalSourcing — '+day+'\n');assert day in body['subject']
  items=[];excluded=0;qualified_blocks=0;pending_blocks=0;used=[]
  for identity in json.loads(r['event_ids']):
   event=events[identity];data=json.loads(event['payload']);p=data['listing'];t=data['tax'];item=dict(data,kind=event['kind'],previous_price=event['previous_price'])
   old=digest.EXCLUDED_ZIPS
   try:
    digest.EXCLUDED_ZIPS=set()
    candidates=[]
    for stats in ({},{'saved_data_only':True}):
     regenerated,_=digest.render(day,[item],stats,{'remaining':0},[])
     card=regenerated.split('\n\n')[2]
     if card in text:candidates.append(card)
    assert candidates,'Frozen event differs from accepted email card'
   finally:digest.EXCLUDED_ZIPS=old
   qualified_blocks+=1;used.append(candidates[0])
   if str(p.get('zipCode','')).split('-')[0] in EXCLUDED:excluded+=1;continue
   observed=data.get('listing_observed_at') or data['verified_at']
   items.append(dict(source_id=p['id'],source='RentCast',source_label='RentCast Sale Listings',address=p['formattedAddress'],street_address=p.get('addressLine1'),unit=p.get('addressLine2'),city=p.get('city'),state=p.get('state'),zip_code=p.get('zipCode'),price=p['price'],bedrooms=p['bedrooms'],bathrooms=p['bathrooms'],square_feet=p['squareFootage'],annual_tax=t['annual'],tax_year=t['year'],tax_status='provider_verified',tax_source='RentCast Property Records',qualification='verified',section=event['kind'],previous_price=event['previous_price'],reasons=[],listing_url=digest.listing_link(p),discovered_at=observed,updated_at=observed,listing_observed_at=data.get('listing_observed_at'),verified_at=data['verified_at'],tax_fetched_at=data['tax_fetched_at']))
  for block in text.split('\n\n'):
   if ' | ZIP ' not in block:continue
   m=pattern.fullmatch(block);assert m,'Unrecognized frozen pending card'
   p=m.groupdict();pending_blocks+=1;used.append(block)
   if p['zip'].split('-')[0] in EXCLUDED:excluded+=1;continue
   tax=None;year=None
   if p['tax']!='not verified':
    mt=re.fullmatch(r'(\d{4}): \$([\d,.]+)',p['tax']);assert mt,'Unrecognized frozen tax evidence';year=int(mt[1]);tax=number(mt[2])
   address=None if p['address']=='Address missing' else p['address'];zipcode=None if p['zip'] in ('Missing','None') else p['zip'];assert address and zipcode,'Pending identity missing from frozen email'
   observed=p['observed'];datetime.datetime.fromisoformat(observed.replace('Z','+00:00'))
   items.append(dict(source_id=None,source='RentCast',source_label='RentCast Sale Listings',address=address,street_address=address.split(',')[0],unit=None,city=None,state=None,zip_code=zipcode,price=number(p['price']),bedrooms=number(p['beds']),bathrooms=number(p['baths']),square_feet=number(p['area']),annual_tax=tax,tax_year=year,tax_status='provider_verified' if tax is not None else 'pending_verification',tax_source='RentCast Property Records' if tax is not None else None,qualification='pending_verification',section='pending_verification',previous_price=None,reasons=p['reasons'].split(', ') if p['reasons'] else [],listing_url=None if p['link']=='Not supplied by provider' else p['link'],discovered_at=observed,updated_at=observed,listing_observed_at=observed,verified_at=None,tax_fetched_at=None))
  for block in used:assert html.escape(block) in body['html'],'Text/HTML selection mismatch'
  for item in items:item['reasons'].append('Historical email import: original first-discovery date unavailable; date shown uses retained observation or verification evidence.')
  assert qualified_blocks==len(json.loads(r['event_ids']))
  assert sum('\nחדרי שינה:' in b for b in text.split('\n\n'))==qualified_blocks,'Unrecovered qualified card'
  report=dict(schema_version=1,report_id=r['id'],report_date=day,prepared_at=r['first_attempt_at'],discovery_state='historical_accepted_email_backfill',opportunities=items,recovery_evidence=dict(source='accepted frozen daily outbox text plus explicitly selected deal_events',email_text_sha256=hashlib.sha256(text.encode()).hexdigest(),email_payload_sha256=hashlib.sha256(r['payload'].encode()).hexdigest(),selected_event_ids_sha256=hashlib.sha256(r['event_ids'].encode()).hexdigest(),timestamp_source='prepared_at uses original persisted first send-attempt timestamp; discovery/updated values use earliest evidence retained in the card',missing_original_metadata=['original first discovery timestamp','provider IDs for pending cards','city/state/unit metadata not printed in pending cards']))
  reports.append(report);summary.append(dict(day=day,qualified_or_price_reduction=qualified_blocks,pending_verification=pending_blocks,excluded=excluded,recoverable_cards=len(items),exact_selection_verified=True,structured_snapshot_originally_available=False))
 return reports,summary

def load_evidence():
 import psycopg
 from psycopg.rows import dict_row
 with psycopg.connect(os.environ['DATABASE_URL'],autocommit=True,connect_timeout=10,row_factory=dict_row) as db:
  db.execute('BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY')
  try:
   db.execute('SET LOCAL statement_timeout=15000')
   db.execute('SET LOCAL lock_timeout=3000')
   reports=db.execute('SELECT * FROM outbox WHERE job_id=ANY(%s::text[]) ORDER BY job_id',(['daily:'+d for d in DAYS],)).fetchall()
   if [r['job_id'] for r in reports]!=['daily:'+d for d in DAYS]:raise BackfillValidationError('Both historical outboxes are required')
   ids=[i for r in reports for i in json.loads(r['event_ids'])]
   events=db.execute('SELECT * FROM deal_events WHERE id=ANY(%s::text[])',(ids,)).fetchall()
   return dict(reports=reports,selected_events=events)
  finally:db.execute('ROLLBACK')

def frozen_bodies(evidence):
 reports,summary=reconstruct(evidence)
 if [r['report_date'] for r in reports]!=list(DAYS):raise BackfillValidationError('Wrong historical dates')
 bodies=[json.dumps(r,ensure_ascii=False,sort_keys=True,separators=(',',':')) for r in reports]
 for report,body in zip(reports,bodies):
  if hashlib.sha256(body.encode()).hexdigest()!=EXPECTED_REPORT_SHA256[report['report_date']]:raise BackfillValidationError('Frozen report differs from verified export: '+report['report_date'])
 return reports,bodies,summary

def normalise(key,value):
    value=value.strip()
    if value.startswith(key+'='):value=value.split('=',1)[1].strip()
    if len(value)>1 and value[0]==value[-1] and value[0] in ('"',"'"):value=value[1:-1]
    if key=='COMMAND_CENTER_SERVICE_TOKEN' and value.lower().startswith('bearer '):value=value[7:].strip()
    if not value or any(c.isspace() for c in value):raise SystemExit('STOP: '+key+' contains blank/multiline input; no request sent')
    return value

def private_value(key):
    value=os.environ.get(key,'')
    if not value:
        with warnings.catch_warnings():
            warnings.simplefilter('error',getpass.GetPassWarning)
            value=getpass.getpass('Paste existing rentalsourcing-daily '+key+' (hidden; never paste into chat): ')
    return normalise(key,value)

def response_data(raw,encoding):
    if encoding.lower()=='gzip' or raw.startswith(b'\x1f\x8b'):raw=gzip.decompress(raw)
    elif encoding.lower()=='deflate':raw=zlib.decompress(raw)
    try:return json.loads(raw)
    except (ValueError,UnicodeError):
        import re,html
        text=raw[:4096].decode('utf-8',errors='replace')
        text=re.sub(r'<(?:script|style)\b[^>]*>.*?</(?:script|style)>',' ',text,flags=re.S|re.I)
        text=html.unescape(re.sub(r'<[^>]*>',' ',text))
        return {'_backfill_non_json':True,'message':'Non-JSON response: '+' '.join(text.split())[:500]}


def safe_error(result):
    if not isinstance(result,dict):return 'non-JSON response (not a receiver authentication receipt)'
    error=result.get('error',result.get('message',result.get('detail',result.get('error_description','unrecognized JSON error'))))
    if isinstance(error,dict):error=error.get('message',error.get('code','structured error'))
    error=str(error).replace(secret,'[REDACTED]').replace(service,'[REDACTED]')
    import re
    error=re.sub(r'https?://\S+','[URL REDACTED]',error)
    error=re.sub(r'[A-Za-z0-9_+/=-]{40,}','[TOKEN REDACTED]',error)
    meta=result.get('_backfill_http_meta','')
    return error[:350].replace('\n',' ')+(' ['+meta+']' if meta else '')

def request(url,body=None):
    headers={'Accept':'application/json','Accept-Encoding':'identity','User-Agent':'RentalSourcing-Historical-Backfill/2','OAI-Sites-Authorization':'Bearer '+service}
    raw=None
    if body is not None:
        stamp=str(int(time.time()));raw=body.encode()
        signature=hmac.new(secret.encode(),stamp.encode()+b'.'+raw,hashlib.sha256).hexdigest()
        headers.update({'Content-Type':'application/json','X-RentalSourcing-Timestamp':stamp,'X-RentalSourcing-Signature':'v1='+signature})
    req=urllib.request.Request(url,data=raw,headers=headers,method='POST' if raw is not None else 'GET')
    try:
        with opener.open(req,timeout=15) as response:
            raw_response=response.read(65537)
            if len(raw_response)>65536:raise SystemExit('STOP: oversized unexpected response')
            return response.status,response_data(raw_response,response.headers.get('Content-Encoding',''))
    except urllib.error.HTTPError as error:
        raw_response=error.read(65537)
        result=response_data(raw_response,error.headers.get('Content-Encoding','')) if len(raw_response)<=65536 else None
        if isinstance(result,dict):
            import re
            ray=re.sub(r'[^a-zA-Z0-9-]','',error.headers.get('CF-Ray',''))[:80]
            result['_backfill_http_meta']='type='+error.headers.get('Content-Type','').split(';')[0][:60]+('; cf-ray='+ray if ray else '')
        return error.code,result
    except Exception as error:raise SystemExit('STOP: '+type(error).__name__+'; no automatic retries') from None


def main(argv=None):
 global secret,service,opener
 parser=argparse.ArgumentParser(description=__doc__)
 parser.add_argument('--execute',action='store_true')
 parser.add_argument('--service-token-sha256',help='Non-secret fingerprint obtained from the current Site; required for execution')
 args=parser.parse_args(argv)
 reports,bodies,summary=frozen_bodies(load_evidence())
 print('DRY_RUN_VERIFIED',json.dumps(summary,ensure_ascii=True))
 print('Verified union: 52 properties; 47 cross-day repeats; 3 excluded cards. No raw inventory used.')
 if not args.execute:return 0
 if not re.fullmatch('[a-f0-9]{64}',args.service_token_sha256 or ''):raise BackfillValidationError('Execution requires the verified Site credential fingerprint')
 secret=private_value('COMMAND_CENTER_SYNC_SECRET')
 service=private_value('COMMAND_CENTER_SERVICE_TOKEN')
 if len(secret)<32:raise BackfillValidationError('Signing secret too short')
 if hashlib.sha256(service.encode()).hexdigest()!=args.service_token_sha256:raise BackfillValidationError('Service credential does not match this Site; no request sent')
 opener=urllib.request.build_opener(NoRedirect)
 code,result=request(URL.replace('/api/rentalsourcing/sync','/api/health'))
 if code!=200 or not isinstance(result,dict) or result.get('ok') is not True:raise SystemExit('STOP: Site access/health; HTTP '+str(code)+'; '+safe_error(result))
 print('SERVICE_ACCESS_VERIFIED')
 for body,report in zip(bodies,reports):
  code,result=request(URL,body)
  if code!=200 or not isinstance(result,dict) or result.get('accepted') is not True or result.get('report_id')!=report['report_id']:raise SystemExit('STOP: receiver '+report['report_date']+'; HTTP '+str(code)+'; '+safe_error(result)+'; no automatic retries')
  print('BACKFILL_ACCEPTED',report['report_date'],'cards='+str(len(report['opportunities'])),'duplicate_report='+str(bool(result.get('duplicate'))))
 print('BACKFILL_COMPLETE')
 return 0

if __name__=='__main__':
 try:sys.exit(main())
 except (BackfillValidationError,AssertionError) as error:
  print('STOP: '+str(error),file=sys.stderr);sys.exit(1)
 except Exception as error:
  # Never dump credentials, source rows, SQL parameters, URLs or tracebacks.
  print('STOP: '+type(error).__name__+'; source validation or local execution failed; nothing was retried',file=sys.stderr)
  sys.exit(1)
