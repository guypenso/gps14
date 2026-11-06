"""Free, bounded off-market intake. Uses the authoritative criteria; never provider/email sends."""
import logging
import datetime as dt,hashlib,hmac,json,re,urllib.request,urllib.robotparser,time,threading,os
from html.parser import HTMLParser
from urllib.parse import urlsplit,urljoin
from .core import verification,tax_verification
from .collector import LIVE
from .provenance import demo_marked
from .geography import POLICY
from .request import NoRedirect
UTC=dt.timezone.utc
PLB='https://pghlandbank.org/pilot-residential-rehab-for-sale/'
SOURCES=[
 {'id':'pittsburgh-land-bank','name':'Pittsburgh Land Bank','url':PLB,'type':'public_disposition','mode':'public_inventory','restrictions':'Individual offering and rehabilitation/program conditions require review. Published agent listings may also be on MLS; not exclusive off-market inventory.'},
 {'id':'tricog-land-bank','name':'Tri-COG Land Bank','url':'https://tricoglandbank.org/properties/','type':'public_disposition','mode':'manual_import_only','restrictions':'Property-specific owner-occupant priority, visit and rehabilitation plan. Interactive inventory; no documented authorized batch feed verified.'},
 {'id':'kozlina','name':'Kozlina Real Estate','url':'https://www.kozlinarealestate.com/properties','type':'wholesaler','mode':'manual_import_only','restrictions':'Terms restrict copying, commercial reuse and sharing. Obtain permission or supplier-provided deal files. No automated scraping.'},
 {'id':'pittsburgh-off-market','name':'Pittsburgh Off Market','url':'https://pittsburghoffmarket.com/','type':'wholesaler','mode':'manual_import_only','restrictions':'Buyers-list access requires owner signup and supplier permission; no documented public feed verified.'},
 {'id':'rapid-real-estate','name':'Rapid Real Estate Buyers','url':'https://www.cheapdiscountproperties.com/','type':'wholesaler','mode':'manual_import_only','restrictions':'Preferred Buyers Club; no documented public feed verified. No automatic subscriptions.'},
 {'id':'wprdc','name':'WPRDC / Allegheny County assessments','url':'https://data.wprdc.org/dataset/property-assessments','type':'public_record','mode':'manual_import_only','restrictions':'Research leads only, never seller intent. Existing cached parcel/tax evidence reused. Bulk research import accepts supplied CSV; no private owner contacts collected.'},
 {'id':'manual','name':'Manual / supplier CSV','url':None,'type':'supplier','mode':'manual_import_only','restrictions':'Import only records and files you are authorized to use.'},
 {'id':'gmail','name':'Selected supplier Gmail intake','url':None,'type':'supplier_email','mode':'requires_account_consent','restrictions':'Disabled per account until explicit sender/label consent and existing Gmail read authorization. No unrelated mailbox scans.'}]
def norm(v):
 from .public_tax import norm as n
 # Preserve units and house numbers; suffix normalization only.
 return ' '.join({'street':'st','avenue':'ave','road':'rd','drive':'dr','boulevard':'blvd'}.get(x,x) for x in n(str(v)).lower().split())
def street_key(p):
 base=norm(str(p.get('addressLine1') or p.get('formattedAddress','').split(',')[0]));unit=norm(p.get('unit') or p.get('addressLine2') or '')
 if unit and 'unit' not in base.split():base+=' '+(unit if unit.startswith('unit ') else 'unit '+unit)
 return base
def evaluate(store,boundary,items,now=None):
 now=now or dt.datetime.now(UTC)
 if not isinstance(items,list) or len(items)>30:raise ValueError('At most 30 supplied records')
 existing=[]
 for identity,raw,observed in store.execute('SELECT id,payload,observed_at FROM properties').fetchall():
  p=json.loads(raw);existing.append((identity,p,observed))
 out=[]
 for item in items:
  p=dict(item.get('property') or {});p['id']=str(p.get('id') or 'offmarket:'+hashlib.sha256(json.dumps(p,sort_keys=True).encode()).hexdigest())
  # An offering is not automatically an active RentCast listing. For evaluation only,
  # explicit available sale evidence supplies the status; speculative leads stay pending.
  if item.get('availability')=='available' and item.get('evidence_url'):p['status']='Active'
  street=street_key(p);zipcode=str(p.get('zipCode') or '')
  matches=[(i,v,o) for i,v,o in existing if street and street_key(v)==street and (not zipcode or str(v.get('zipCode'))==zipcode)]
  match=None
  if len(matches)==1:
   identity,cached,observed=matches[0];match={'source_id':identity,'observed_at':observed,'match':'unique_exact_street_unit_zip' if zipcode else 'unique_exact_street_unit_city_requires_review'}
   # Never overwrite supplier price/status with another source's offer.
   for k in ('latitude','longitude','bedrooms','bathrooms','squareFootage','propertyType','propertySubType','zipCode','city','state','addressLine1'):
    if p.get(k) in (None,''):p[k]=cached.get(k)
   if not p.get('formattedAddress') or ',' not in p['formattedAddress']:p['formattedAddress']=cached.get('formattedAddress',p.get('formattedAddress'))
  state,reasons=verification(p,boundary,profile='offmarket',offering=item.get('kind')=='offering');tax=None;estimate=None
  if match and match['match']=='unique_exact_street_unit_city_requires_review':
   reasons.append('cached_address_match_requires_review')
   if state=='Candidate':state='Pending Verification'
  base_state=state
  if match:
   saved=store.get_tax(match['source_id'],now.isoformat())
   if saved:
    raw=saved[0]
    if isinstance(raw,str):raw=json.loads(raw)
    if isinstance(raw,dict) and raw.get('id')==match['source_id'] and saved[1]==LIVE and not demo_marked(raw):
     ts,tax,tr=tax_verification(raw,now.date())
     if ts=='Rejected':state='Rejected';reasons.append(tr)
     elif ts!='Qualified':tax=None
   row=store.execute('SELECT payload FROM settings WHERE name=?',('public_tax_batch:2026',)).fetchone()
   if row:estimate=json.loads(row[0]).get('estimates',{}).get(match['source_id'])
  from .offmarket_intelligence import analyze
  evidence=[{k:v for k,v in e.items() if k!='program_inventory_verified'} for e in (item.get('evidence') or [])]
  # Only this fixed, fetched public inventory can establish its program contact.
  # Never copy another property's agent or infer an asking price from cached listings.
  if item.get('source_id')=='pittsburgh-land-bank':
   saved_contact=store.execute('SELECT payload FROM settings WHERE name=?',('offmarket_plb_contacts_v2',)).fetchone()
   if saved_contact:
    contacts=json.loads(saved_contact[0]).get('contacts',{})
    route=contacts.get(str(item.get('external_id')))
    if route and route.get('offering_street_key')==street_key(p):evidence.append({**route,'matched_property':p,'program_inventory_verified':True})
  tax_info={'state':'verified' if tax else 'estimated' if estimate and estimate.get('state')=='estimated' else 'unknown','verified':tax,'estimate':estimate,'source':saved[1] if match and saved and tax else None,'retrieved_at':saved[2] if match and saved and tax else None}
  if state=='Rejected':base_state='Rejected'
  decision=analyze(p,item,base_state,reasons,tax_info,evidence,(cached,match['observed_at']) if match else None,now)
  out.append({'property':p,'criteria_version':POLICY,'criteria_state':decision['state'],'reasons':decision['reasons'],'tax':tax_info,'match':match,'score':decision['score'],'score_explanation':decision['score_explanation'],'evaluated_at':now.isoformat(),'acquisition':decision})
 return out
class Inventory(HTMLParser):
 def __init__(self):super().__init__(convert_charrefs=True);self.links=[];self.href=None;self.label=[];self.text=[]
 def handle_starttag(self,tag,attrs):
  if tag=='a':self.href=dict(attrs).get('href');self.label=[]
 def handle_data(self,data):
  self.text.append(data)
  if self.href:self.label.append(data)
 def handle_endtag(self,tag):
  if tag=='a' and self.href:self.links.append((self.href,' '.join(self.label).strip()));self.href=None;self.label=[]
def parse_public(html,now):
 parser=Inventory();parser.feed(html);records=[]
 for href,label in parser.links:
  if 'LISTING' not in label.upper() or not re.match(r'^\d',label):continue
  address=re.split(r'\s+LISTING',label,flags=re.I)[0].strip();available=not re.search(r'PENDING|UNDER CONTRACT|SOLD',label,re.I)
  u=urljoin(PLB,href)
  if urlsplit(u).scheme!='https':continue
  records.append({'source_id':'pittsburgh-land-bank','external_id':hashlib.sha256((address+'|'+u).encode()).hexdigest(),'kind':'offering','availability':'available' if available else 'unavailable','evidence_url':PLB,'listing_url':u,'purchase_eligibility':'unknown','rights':'public_disposition','restrictions':'Rehabilitation/application conditions and investor-use eligibility require individual confirmation. May also be publicly listed. No title or enforceability conclusion.','received_at':now.isoformat(),'property':{'formattedAddress':address+', Pittsburgh, PA','addressLine1':address,'city':'Pittsburgh','state':'PA'}})
 return records

def InventoryText(html):
 p=Inventory();p.feed(html);return p.text

def fetch_public(now=None,opener=None):
 now=now or dt.datetime.now(UTC);opener=opener or urllib.request.build_opener(NoRedirect)
 def read(url,limit):
  with opener.open(urllib.request.Request(url,headers={'User-Agent':'PENTEN-RE-OffMarket/1.0 public inventory reader','Accept':'text/html,text/plain'}),timeout=15) as r:
   if r.status!=200:raise ValueError('public_source_http_'+str(r.status))
   raw=r.read(limit+1)
   if len(raw)>limit:raise ValueError('Public source oversized')
   return raw.decode('utf-8','replace')
 # Honor published access controls; unavailable robots is not assumed permission.
 rules=read('https://pghlandbank.org/robots.txt',100000);robots=urllib.robotparser.RobotFileParser();robots.parse(rules.splitlines())
 if not robots.can_fetch('PENTEN-RE-OffMarket/1.0',PLB):raise ValueError('Public source disallows automated access')
 html=read(PLB,1500000);records=parse_public(html,now)
 if not records:raise ValueError('Inventory parser found no recognizable offering links; not a successful empty inventory')
 return {'source_id':'pittsburgh-land-bank','state':'fetched','attempted_at':now.isoformat(),'fetched_at':dt.datetime.now(UTC).isoformat(),'records':records[:30],'received':len(records),'partial':len(records)>30,'program_contact_confirmed':bool(re.search(r'Please\s+contact\s+Pittsburgh\s+Land\s+Bank\s+staff\s+with\s+any\s+additional\s+questions', ' '.join(InventoryText(html)),re.I) and 'staff@pghlandbank.org' in html and '412-255-6566' in html),'source_sha256':hashlib.sha256(html.encode()).hexdigest()}

def cache_program_contacts(store,result):
 # Source snapshot is bounded, robots-checked and fetched by fetch_public, not supplied by a client.
 records=result['records'];contacts={}
 if result.get('program_contact_confirmed'):
  for item in records:
   contacts[item['external_id']]={'offering_street_key':street_key(item['property']),'kind':'contact','name':'Pittsburgh Land Bank','role':'Public Agency','email':'staff@pghlandbank.org','phone':'412-255-6566','source_url':PLB,'observed_at':result['attempted_at'],'verification_method':'official_inventory','relationship':'Responsible for inquiries about this property in the published residential rehab sales program','relationship_confirmed':True}
 store.save_setting('offmarket_plb_contacts_v2',{'contacts':contacts,'source_sha256':result.get('source_sha256'),'at':result['attempted_at']})

def next_source_refresh(now,result):
 """Existing worker drains bounded pages, then checks again before the report."""
 from .schedule import ISRAEL
 if result.get('state')=='partial':return (now+dt.timedelta(minutes=5)).isoformat()
 local=now.astimezone(ISRAEL)
 cutoff=local.replace(hour=7,minute=45,second=0,microsecond=0)
 if cutoff<=local:cutoff+=dt.timedelta(days=1)
 return min(now+dt.timedelta(hours=6),cutoff.astimezone(UTC)).isoformat()

def claim_schedule(store,now):
 from .sync_runtime import get
 # Separate advisory lock: off-market refresh never waits for or takes the paid discovery lock.
 if store.pg and not store.execute('SELECT pg_try_advisory_lock(73541211)').fetchone()[0]:return False
 previous=get(store,'offmarket_scheduler',{})
 from .schedule import ISRAEL
 local=now.astimezone(ISRAEL);day=local.date().isoformat()
 pre_report=local.hour==7 and local.minute>=45 and get(store,'listing_pre_report_day')!=day
 if previous.get('next_at','')>now.isoformat() and not pre_report:return False
 if pre_report:store.save_setting('listing_pre_report_day',day)
 store.save_setting('offmarket_scheduler',{'attempted_at':now.isoformat(),'next_at':(now+dt.timedelta(hours=6)).isoformat(),'state':'running','validation_id':previous.get('validation_id')})
 return True

def request_listing_validation(store,now):
 """Arm one safe pass of the existing worker; never execute discovery here."""
 from .sync_runtime import get
 key='realscout_scheduler_validation_v1';old=get(store,key)
 if old:return old
 if store.pg and not store.execute('SELECT pg_try_advisory_lock(73541211)').fetchone()[0]:raise ValueError('Source scheduler is busy')
 previous=get(store,'offmarket_scheduler',{})
 if previous.get('state')=='running':raise ValueError('Source scheduler is busy')
 request={'id':'realscout-validation-20261106-v1','state':'queued','requested_at':now.isoformat(),'original_next_at':previous.get('next_at')}
 store.save_setting(key,request)
 store.save_setting('offmarket_scheduler',{**previous,'next_at':now.isoformat(),'state':'validation_queued','validation_id':request['id']})
 return request

def complete_listing_validation(store,request,result,now):
 """Record the worker receipt and restore its original six-hour cadence."""
 email=result.get('email',{}).get('results',[])
 passed=result.get('state')=='success' and bool(email) and all(r.get('state')=='success' for r in email)
 store.save_setting('realscout_scheduler_validation_v1',{**request,'state':'done' if passed else 'failed','completed_at':dt.datetime.now(UTC).isoformat(),'result':result})
 return request.get('original_next_at') if (request.get('original_next_at') or '')>now.isoformat() else (now+dt.timedelta(hours=6)).isoformat()

def start_scheduler(url):
 """Independent six-hour lease in the existing dashboard; no new paid service."""
 from .offmarket_intelligence import VERSION
 if os.getenv('ENABLE_REVIEW_INVENTORY_SYNC','false').lower()!='true':return
 def loop():
  from .storage import Store
  from .sync_runtime import get,request_json,SyncFailure
  from .command_center import review_configuration
  while True:
   s=None
   try:
    s=Store(url,initialize=False);now=dt.datetime.now(UTC)
    from .investment_policy import POLICY_VERSION
    if not get(s,POLICY_VERSION):
     from .criteria import reevaluate
     from .geography import load_boundary
     from .config import Settings
     changed=reevaluate(s,load_boundary(Settings.from_env().boundary_file),now)
     s.save_setting(POLICY_VERSION,{'at':now.isoformat(),'records_updated':changed,'provider_calls':0})
    # Short, persistent claim; no scan/email paths, no shared discovery lock held over HTTP.
    if not get(s,'offmarket_reconciliation_v5'):
     u,k,t=review_configuration()
     # Fetch program evidence locally. A new worker may start before Render switches
     # the public backend, so a loopback refresh can otherwise hit the old release.
     if not get(s,'offmarket_plb_contacts_v2'):
      cache_program_contacts(s,fetch_public(now))
     cursor=get(s,'offmarket_reconcile_cursor_v5','');body=json.dumps({'action':'reconcile','cursor':cursor},separators=(',',':')).encode()
     def reconciled(value,*args):
      if value.get('state')!='success':raise ValueError('Off-market reconciliation incomplete')
      if value.get('summary',{}).get('policy_version')!=VERSION:raise SyncFailure('offmarket_policy_handover',None,True)
     result=request_json(s,'offmarket_reconcile','stored:v5:'+cursor,(u.rsplit('/',1)[0]+'/offmarket-refresh',k,t),body,timeout=90,validate=reconciled)
     s.save_setting('offmarket_reconcile_cursor_v5',result.get('next_cursor') or '')
     if not result.get('next_cursor'):s.save_setting('offmarket_reconciliation_v5',result)
     logging.getLogger('rentalsourcing.reliability').info('OFFMARKET_RECONCILED '+json.dumps(result,sort_keys=True))
    if claim_schedule(s,now):
     validation=get(s,'realscout_scheduler_validation_v1') if get(s,'offmarket_scheduler',{}).get('validation_id') else None
     s.close();s=Store(url,initialize=False)
     operation=('validation:'+validation['id']) if validation else now.strftime('%Y-%m-%d:%H')
     u,k,t=review_configuration();body=json.dumps({'action':'refresh','operation_id':operation},separators=(',',':')).encode()
     def receipt(value,*args):
      if value.get('state') not in ('success','partial'):raise ValueError('Off-market refresh not completed')
     result=request_json(s,'offmarket_refresh',operation,(u.rsplit('/',1)[0]+'/offmarket-refresh',k,t),body,timeout=90,validate=receipt)
     logging.getLogger('rentalsourcing.reliability').info('OFFMARKET_REFRESH '+json.dumps({**result,'scheduler_validation_id':validation['id'] if validation else None},sort_keys=True))
     if result.get('state')=='partial':
      from .sync_runtime import component,incident
      error={'category':'offmarket_source_incomplete','retryable':True}
      component(s,'offmarket_refresh','partial',pending=1,error=error);incident(s,'offmarket_refresh',error)
     next_at=complete_listing_validation(s,validation,result,now) if validation else next_source_refresh(now,result)
     s.save_setting('offmarket_scheduler',{'attempted_at':now.isoformat(),'next_at':next_at,'state':result.get('state','failed'),'result':result})
   except Exception as e:
    logging.getLogger('rentalsourcing.reliability').warning('OFFMARKET_REFRESH_FAILED '+type(e).__name__)
    if s:
     s.save_setting('offmarket_scheduler',{'attempted_at':dt.datetime.now(UTC).isoformat(),'next_at':(dt.datetime.now(UTC)+dt.timedelta(minutes=30)).isoformat(),'state':'failed','error_type':type(e).__name__})
   finally:
    if s:s.close()
   time.sleep(300)
 threading.Thread(target=loop,name='offmarket-free-discovery',daemon=True).start()
