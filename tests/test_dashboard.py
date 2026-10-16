import datetime as dt
import io
import json
import tempfile
import unittest
from rentalsourcing.dashboard import Dashboard, handler_for
from rentalsourcing.storage import Store


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.url = 'sqlite:///' + self.temp.name + '/db'
        store = Store(self.url)
        store.migrate()
        store.save_property('live-property',{'formattedAddress':'Real stored address','price':150000},'Pending Verification',['missing_tax_history'],'rentcast-live',1,store.now())
        store.observe({'id':'actual-record','formattedAddress':'<script>alert(1)</script>', 'price':150000, '_validation':{'city_boundary':True,'tax':{'annual':1000}}, 'secret':'must-not-appear'})
        store.run('run-id', {'status':'complete','qualified':0})
        store.close()
        self.app = Dashboard(self.url, 'x'*32, production=False)
        self.handler = handler_for(self.app)

    def tearDown(self):
        self.temp.cleanup()

    def get(self,path,token=None):
        request = self.handler.__new__(self.handler)
        request.path = path
        request.headers = {'Authorization':'Bearer '+token} if token else {}
        request.wfile = io.BytesIO()
        result = {'headers':{}}
        request.send_response = lambda code: result.update(status=code)
        request.send_header = lambda name,value: result['headers'].update({name:value})
        request.end_headers = lambda: None
        request.do_GET()
        return result['status'],request.wfile.getvalue(),result['headers']

    def test_requires_auth_and_disallows_url_token(self):
        self.assertEqual(self.get('/api/status')[0],401)
        self.assertEqual(self.get('/api/status','bad')[0],401)
        self.assertEqual(self.get('/api/status?token='+'x'*32)[0],400)

    def test_authenticated_snapshot_exposes_only_stored_allowlisted_data(self):
        status,body,headers=self.get('/api/status','x'*32)
        self.assertEqual(status,200)
        data=json.loads(body)
        self.assertEqual(data['properties'][0]['state'],'Pending Verification')
        self.assertIsNone(data['properties'][0]['tax'])
        self.assertEqual(data['observations'][0]['id'],'actual-record')
        self.assertEqual(data['observations'][0]['_validation']['tax']['annual'],1000)
        self.assertNotIn(b'must-not-appear',body)
        self.assertEqual(headers['Cache-Control'],'no-store')
        self.assertIn('frame-ancestors',headers['Content-Security-Policy'])

    def test_tax_history_is_displayed_without_promoting_pending_property(self):
        store=Store(self.url)
        store.save_tax('live-property',{'propertyTaxes':{'2025':{'year':2025,'total':1100}}},'rentcast-live',store.now(),'2099-01-01T00:00:00+00:00')
        store.close()
        status,body,_=self.get('/api/status','x'*32)
        self.assertEqual(status,200)
        row=json.loads(body)['properties'][0]
        self.assertEqual(row['tax']['adjusted'],1200)
        self.assertEqual(row['state'],'Pending Verification')
        self.assertEqual(row['tax_source'],'rentcast-live')

    def test_qualified_state_requires_fresh_listing_and_live_unexpired_tax(self):
        now=dt.datetime.now(dt.timezone.utc)
        cases=[('valid',0,1,'rentcast-live',True,'Qualified'),
               ('expired-tax',0,-1,'rentcast-live',True,'Pending Verification'),
               ('missing-tax',0,1,'rentcast-live',False,'Pending Verification'),
               ('fixture-tax',0,1,'fixture',True,'Pending Verification'),
               ('stale-listing',25,1,'rentcast-live',True,'Pending Verification')]
        store=Store(self.url)
        for identity,age,expiry,source,with_tax,expected in cases:
            store.save_property(identity,{'formattedAddress':identity,'price':150000},'Qualified',[],'rentcast-live',1,(now-dt.timedelta(hours=age)).isoformat())
            if with_tax:
                store.save_tax(identity,{'propertyTaxes':{str(now.year-1):{'year':now.year-1,'total':1000}}},source,(now-dt.timedelta(minutes=1)).isoformat(),(now+dt.timedelta(hours=expiry)).isoformat())
        store.close()
        rows={p['id']:p for p in self.app.snapshot()['properties']}
        for identity,age,expiry,source,with_tax,expected in cases:
            with self.subTest(identity=identity):
                self.assertEqual(rows[identity]['state'],expected)
                self.assertEqual(rows[identity]['stored_state'],'Qualified')
                if identity in ('expired-tax','missing-tax','fixture-tax'):
                    self.assertIsNone(rows[identity]['tax'])
                    self.assertFalse(rows[identity]['tax_current'])
                if expected=='Pending Verification': self.assertTrue(rows[identity]['reasons'])

    def test_health_and_safe_rendering(self):
        self.assertEqual(self.get('/healthz')[0],200)
        script=self.get('/app.js')[1]
        self.assertIn(b'textContent',script)
        self.assertNotIn(b'innerHTML',script)
        self.assertNotIn(b'localStorage',script)
        self.assertNotIn(b'x'*32,self.get('/')[1])

    def test_production_rejects_sqlite_and_weak_token(self):
        with self.assertRaises(ValueError): Dashboard(self.url,'x'*32)
        with self.assertRaises(ValueError): Dashboard(self.url,'weak',production=False)

    def test_reads_do_not_mutate_database(self):
        from pathlib import Path
        path=Path(self.temp.name+'/db')
        before=path.read_bytes()
        self.app.snapshot()
        self.app.healthy()
        self.assertEqual(before,path.read_bytes())

    def test_database_errors_are_redacted(self):
        self.app.url='sqlite:///'+self.temp.name+'/does-not-exist'
        status,body,_=self.get('/api/status','x'*32)
        self.assertEqual(status,503)
        self.assertNotIn(self.temp.name.encode(),body)
        self.assertEqual(self.get('/healthz')[0],503)


if __name__=='__main__': unittest.main()
