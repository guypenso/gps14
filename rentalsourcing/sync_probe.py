"""Read-only signed endpoint diagnostics. Never sends reports, emails or provider requests."""
import datetime as dt,hashlib,hmac,json,urllib.error,urllib.request,uuid
from urllib.parse import urlsplit
from .command_center import configuration,review_configuration,signed_request
from .request import NoRedirect

def probe(config,opener=None):
 if not config:return {'state':'disabled'}
 url,key,token=config;path='/review-classifications' if url.endswith('/review-sync') else '/classifications';url=url.rsplit('/',1)[0]+path
 nonce=uuid.uuid4().hex;body=json.dumps({'nonce':nonce,'cursor':None},separators=(',',':')).encode();req=signed_request((url,key,token),body,dt.datetime.now(dt.timezone.utc));base={'host':urlsplit(url).hostname,'endpoint':urlsplit(url).path}
 try:
  with (opener or urllib.request.build_opener(NoRedirect)).open(req,timeout=20) as r:
   raw=r.read(1024*1024+1);d=json.loads(raw)
   signature=r.headers.get('X-RentalSourcing-Response-Signature','');valid=hmac.compare_digest(signature,'v1='+hmac.new(key.encode(),raw,hashlib.sha256).hexdigest()) and d.get('nonce')==nonce
   return dict(base,state='authenticated' if valid else 'invalid_signed_response',http_status=r.status,decisions=len(d.get('decisions',[])) if valid else None)
 except urllib.error.HTTPError as e:
  # No bodies, headers, URLs with queries, secrets or decision contents in diagnostics.
  return dict(base,state='http_error',http_status=e.code,category='authentication' if e.code in (401,403) else 'configuration' if e.code in (404,503) else 'server' if e.code>=500 else 'validation')
 except Exception as e:return dict(base,state='unavailable',error_type=type(e).__name__)

def inspect():return {'daily_credentials':probe(configuration()),'review_credentials':probe(review_configuration())}
