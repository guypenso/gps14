"""Focused offline tests; contains only fictional data and no provider calls."""
import contextlib,copy,datetime as dt,hashlib,importlib.util,io,json,pathlib,sys,types,unittest
from unittest.mock import patch,Mock
ROOT=pathlib.Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('historical_backfill_runner',ROOT/'scripts/backfill_accepted_reports.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class BackfillRunnerTests(unittest.TestCase):
 def pending_evidence(self,zip_code='15205'):
  at='2026-10-27T06:00:00+00:00';day='2026-10-27'
  listing=dict(id='synthetic-property',formattedAddress='123 Example Street, Pittsburgh, PA '+zip_code,price=100000,bedrooms=3,bathrooms=1,squareFootage=1200,zipCode=zip_code,status='Active')
  entry=dict(listing=listing,tax=None,reasons=['Tax not verified'],observed_at=at)
  with patch.object(m.digest,'EXCLUDED_ZIPS',{'15203','15204','15210'}):text,html=m.digest.render(day,[],{'pending_properties':[entry]},{'remaining':0},[])
  record=dict(job_id='daily:'+day,id='daily-'+hashlib.sha256(('daily:'+day).encode()).hexdigest(),state='sent',provider_id='fictional-receipt',first_attempt_at=at,event_ids='[]',payload=json.dumps(dict(subject='RentalSourcing '+day,text=text,html=html)))
  return dict(reports=[record],selected_events=[])
 def test_exact_pending_card_and_unknown_data_are_preserved(self):
  reports,_=m.reconstruct(self.pending_evidence());p=reports[0]['opportunities'][0]
  self.assertEqual(p['price'],100000);self.assertIsNone(p['annual_tax']);self.assertIsNone(p['source_id']);self.assertEqual(p['qualification'],'pending_verification')
 def test_excluded_zip_never_enters_payload(self):
  reports,summary=m.reconstruct(self.pending_evidence('15211'));self.assertEqual(reports[0]['opportunities'],[]);self.assertEqual(summary[0]['excluded'],1)
 def test_missing_acceptance_and_changed_html_fail_closed(self):
  data=self.pending_evidence();data['reports'][0]['state']='pending'
  with self.assertRaises(AssertionError):m.reconstruct(data)
  data=self.pending_evidence();body=json.loads(data['reports'][0]['payload']);body['html']='unrelated';data['reports'][0]['payload']=json.dumps(body)
  with self.assertRaises(AssertionError):m.reconstruct(data)
 def test_default_dry_run_does_not_request_credentials_or_http(self):
  with patch.object(m,'load_evidence',return_value={}),patch.object(m,'frozen_bodies',return_value=([],[],[])),patch.object(m,'private_value') as prompt,patch.object(m,'request') as send,contextlib.redirect_stdout(io.StringIO()):
   self.assertEqual(m.main([]),0);prompt.assert_not_called();send.assert_not_called()
 def test_token_normalization_is_exact_and_whitespace_rejected(self):
  token='a'*64
  for value in [token,' Bearer '+token+' ', 'COMMAND_CENTER_SERVICE_TOKEN="'+token+'"']:
   self.assertEqual(m.normalise('COMMAND_CENTER_SERVICE_TOKEN',value),token)
  with self.assertRaises(SystemExit):m.normalise('COMMAND_CENTER_SERVICE_TOKEN','wrong token')
 def test_frozen_report_changes_rejected(self):
  reports=[dict(report_date=d,opportunities=[]) for d in m.DAYS]
  expected={r['report_date']:hashlib.sha256(json.dumps(r,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest() for r in reports}
  with patch.object(m,'reconstruct',return_value=(reports,[])),patch.object(m,'EXPECTED_REPORT_SHA256',expected):
   m.frozen_bodies({});reports[0]['opportunities'].append({'changed':True})
   with self.assertRaises(m.BackfillValidationError):m.frozen_bodies({})
 def test_float8_integral_values_match_export_without_replacing_hashes(self):
  expected_reports=[dict(report_date=d,opportunities=[dict(previous_price=120000,annual_tax=1200.0)]) for d in m.DAYS]
  expected={r['report_date']:hashlib.sha256(json.dumps(r,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest() for r in expected_reports}
  driver_reports=copy.deepcopy(expected_reports)
  for r in driver_reports:r['opportunities'][0]['previous_price']=120000.0
  with patch.object(m,'reconstruct',return_value=(driver_reports,[])),patch.object(m,'EXPECTED_REPORT_SHA256',expected):
   restored,bodies,_=m.frozen_bodies({});self.assertEqual(restored,expected_reports)
   self.assertTrue(all('120000.0' not in body for body in bodies))
   driver_reports[0]['opportunities'][0]['previous_price']=120000.01
   with self.assertRaises(m.BackfillValidationError):m.frozen_bodies({})
 def test_canonicalization_does_not_round_or_convert_strings(self):
  self.assertEqual([m.canonical_relational_number(v) for v in [1.0,1.25,'1.0',None,True]],[1,1.25,'1.0',None,True])
  for value in [float('nan'),float('inf'),float('-inf')]:
   with self.assertRaises(m.BackfillValidationError):m.canonical_relational_number(value)
 def run_execution(self,replies):
  reports=[dict(report_date=d,report_id='fixture-'+d,opportunities=[]) for d in m.DAYS];bodies=['fixture27','fixture28'];token='t'*64
  output=io.StringIO()
  with patch.object(m,'load_evidence',return_value={}),patch.object(m,'frozen_bodies',return_value=(reports,bodies,[])),patch.object(m,'private_value',side_effect=['s'*64,token]),patch.object(m,'request',side_effect=replies) as send,contextlib.redirect_stdout(output):
   try:result=m.main(['--execute','--service-token-sha256',hashlib.sha256(token.encode()).hexdigest()])
   except SystemExit:result='stopped'
   return result,send.call_args_list,output.getvalue()
 def test_single_line_execute_accepts_idempotent_receipts(self):
  replies=[(200,{'ok':True})]+[(200,dict(accepted=True,report_id='fixture-'+d,duplicate=True)) for d in m.DAYS]
  result,calls,out=self.run_execution(replies);self.assertEqual(result,0);self.assertEqual(len(calls),3);self.assertIn('BACKFILL_COMPLETE',out);self.assertNotIn('s'*64,out)
 def test_no_retries_or_success_after_partial_rejection(self):
  result,calls,out=self.run_execution([(200,{'ok':True}),(200,dict(accepted=True,report_id='fixture-2026-10-27')),(409,{'error':'Frozen report changed'})])
  self.assertEqual(result,'stopped');self.assertEqual(len(calls),3);self.assertNotIn('BACKFILL_COMPLETE',out)
 def test_sql_access_is_read_only_and_never_loads_inventory(self):
  statements=[];records=[dict(job_id='daily:'+d,event_ids='[]') for d in m.DAYS]
  class DB:
   def __enter__(self):return self
   def __exit__(self,*args):pass
   def execute(self,sql,args=None):
    statements.append(sql);return types.SimpleNamespace(fetchall=lambda:records if 'FROM outbox' in sql else [])
  pg=types.ModuleType('psycopg');pg.connect=lambda *a,**k:DB();rows=types.ModuleType('psycopg.rows');rows.dict_row=object()
  with patch.dict(sys.modules,{'psycopg':pg,'psycopg.rows':rows}),patch.dict('os.environ',{'DATABASE_URL':'postgresql://fictional/fixture'}):m.load_evidence()
  self.assertIn('READ ONLY',statements[0]);self.assertEqual(statements[-1],'ROLLBACK');self.assertFalse(any('properties' in s or 'INSERT' in s or 'UPDATE' in s for s in statements))

if __name__=='__main__':unittest.main()
