import json,hmac,hashlib
from rentalsourcing.dashboard import create_app
from conftest import listing

def test_backend_health_is_signed_private_and_never_exposes_property_or_mail_contents(store,boundary,clock,monkeypatch):
 key='fixture-health-key'*4;monkeypatch.setenv('COMMAND_CENTER_REVIEW_SECRET',key);monkeypatch.setenv('DASHBOARD_PASSWORD','fixture-dashboard-password-not-real')
 app=create_app(store.test_url,testing=True,clock=clock,boundary=boundary);client=app.test_client()
 assert client.post('/api/operations/backend-health',json={'action':'health','nonce':'a'*32}).status_code==401
 raw=json.dumps({'action':'health','nonce':'a'*32},separators=(',',':'));stamp=str(int(clock().timestamp()));headers={'Content-Type':'application/json','X-RentalSourcing-Timestamp':stamp,'X-RentalSourcing-Signature':'v1='+hmac.new(key.encode(),(stamp+'.'+raw).encode(),hashlib.sha256).hexdigest()}
 response=client.post('/api/operations/backend-health',data=raw,headers=headers)
 assert response.status_code==200;assert response.json['nonce']=='a'*32
 assert response.headers['X-RentalSourcing-Response-Signature']=='v1='+hmac.new(key.encode(),response.data,hashlib.sha256).hexdigest()
 assert key not in response.text and 'DASHBOARD_PASSWORD' not in response.text
 assert client.get('/api/operations').status_code==401
 assert store.execute('SELECT COUNT(*) FROM usage').fetchone()[0]==0
