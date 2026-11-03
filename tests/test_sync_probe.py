import json,hmac,hashlib,urllib.error
from rentalsourcing.sync_probe import probe
from conftest import Response
CONFIG=('https://penten.example.test/api/rentalsourcing/sync','x'*40,'private-test-token')
def test_probe_is_read_only_and_authenticates_feedback():
 class Provider:
  def open(self,req,timeout):
   assert req.full_url.endswith('/classifications');assert timeout==20
   data=json.loads(req.data);raw=json.dumps({'nonce':data['nonce'],'decisions':[]});r=Response({});r.read=lambda *a:raw.encode();r.headers={'X-RentalSourcing-Response-Signature':'v1='+hmac.new(CONFIG[1].encode(),raw.encode(),hashlib.sha256).hexdigest()};return r
 assert probe(CONFIG,Provider())['state']=='authenticated'
def test_error_is_sanitized_and_status_is_not_lost():
 class Provider:
  def open(self,req,timeout):raise urllib.error.HTTPError(req.full_url,401,'private-test-token',{},None)
 result=probe(CONFIG,Provider());assert result['http_status']==401;assert 'private-test-token' not in json.dumps(result)
def test_no_config_makes_no_requests():assert probe(None)=={'state':'disabled'}
