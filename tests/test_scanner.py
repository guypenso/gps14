import datetime as dt
import json
import os
import socket
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch
from rentalsourcing.core import Boundary,basic_rejection,latest_tax
from rentalsourcing.storage import Store,BudgetExceeded
from rentalsourcing.cloud import scan,Client
from rentalsourcing.request import diagnose
from rentalsourcing.provenance import LiveRecord

ROW=dict(id='property-1',formattedAddress='Test fixture only',state='PA',zipCode='15212',status='Active',
         bedrooms=3,bathrooms=1,squareFootage=1000,price=200000,longitude=-80,latitude=40.45)
TAX={'id':'property-1','propertyTaxes':{'2025':{'year':2025,'total':1899}}}
class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store('sqlite:///'+self.tmp.name+'/test.db')
        self.path=Path(self.tmp.name)/'boundary.json'
        self.path.write_text(json.dumps({'type':'Polygon','coordinates':[
            [[-80.1,40.4],[-79.9,40.4],[-79.9,40.6],[-80.1,40.6],[-80.1,40.4]],
            [[-80.05,40.5],[-79.95,40.5],[-79.95,40.55],[-80.05,40.55],[-80.05,40.5]]]}))
        self.boundary=Boundary(self.path)
    def tearDown(self): self.store.close();self.tmp.cleanup()
    def test_basic_and_boundary(self):
        self.assertIsNone(basic_rejection(ROW))
        for change in [dict(zipCode='15210'),dict(zipCode='15203'),dict(price=200001),dict(bedrooms=2),
                       dict(bathrooms=None),dict(squareFootage=999)]:
            self.assertIsNotNone(basic_rejection(dict(ROW,**change)))
        self.assertTrue(self.boundary.contains(ROW))
        for change in [dict(latitude=40.52),dict(latitude=40.7),dict(longitude=None),dict(longitude=-80.1)]:
            self.assertFalse(self.boundary.contains(dict(ROW,**change)))
    def test_tax_strict_threshold_and_missing_latest(self):
        self.assertEqual(latest_tax(TAX)['adjusted'],1999)
        for value in [1900,None,-1,True,float('nan')]:
            self.assertIsNone(latest_tax({'propertyTaxes':{'2025':{'year':2025,'total':value}}}))
        self.assertIsNone(latest_tax({'propertyTaxes':{'2024':{'year':2024,'total':100},'2025':{}}}))
    def test_persistent_atomic_budget(self):
        now=dt.datetime(2026,10,15,tzinfo=dt.timezone.utc)
        self.store.reserve(1,2,now)
        with self.assertRaises(BudgetExceeded):self.store.reserve(1,2,now)
        # Failed daily reservation must not increment the month counter.
        self.assertEqual(self.store.execute('SELECT used FROM usage WHERE period=?',('month:2026-10',)).fetchone()[0],1)
        other=Store('sqlite:///'+self.tmp.name+'/test.db')
        with self.assertRaises(BudgetExceeded):other.reserve(1,2,now)
        other.close()
    def test_scan_dedup_across_runs(self):
        class Fake:
            def get(self,path,params=None):
                return ([LiveRecord(ROW)],'1') if path=='/listings/sale' else (LiveRecord(TAX),None)
        with patch('rentalsourcing.cloud.send_email') as mail:
            first=scan(self.store,self.boundary,Fake(),True)
            second=scan(self.store,self.boundary,Fake(),True)
            self.assertEqual(first['sent'],1);self.assertEqual(second['sent'],0)
            mail.assert_called_once()
    def test_uncertain_delivery_never_auto_repeated(self):
        class Fake:
            def get(self,path,params=None):return ([LiveRecord(ROW)],'1') if path=='/listings/sale' else (LiveRecord(TAX),None)
        with patch('rentalsourcing.cloud.send_email',side_effect=TimeoutError):
            with self.assertRaises(TimeoutError):scan(self.store,self.boundary,Fake(),True)
        with patch('rentalsourcing.cloud.send_email') as mail:
            scan(self.store,self.boundary,Fake(),True);mail.assert_not_called()
        self.assertEqual(self.store.execute('SELECT state FROM notifications').fetchone()[0],'unknown')
    def test_transport_error_counts_once_no_retry_and_redacts(self):
        with patch.dict(os.environ,{'ENABLE_RENTCAST':'true','RENTCAST_API_KEY':'fake-test-key'}):
            client=Client(self.store)
            with patch.object(client.opener,'open',side_effect=urllib.error.URLError(socket.gaierror(-2,'DNS failure'))) as transport:
                with self.assertRaises(urllib.error.URLError):client.get('/listings/sale',{})
                self.assertEqual(transport.call_count,1)
            self.assertEqual(client.count,1)
            self.assertNotIn('fake-test-key',str(diagnose(urllib.error.URLError('fake-test-key'))))
    def test_missing_tax_does_not_send(self):
        class Fake:
            def get(self,path,params=None):return ([ROW],'1') if path=='/listings/sale' else ({'id':'property-1'},None)
        with patch('rentalsourcing.cloud.send_email') as mail:
            result=scan(self.store,self.boundary,Fake(),True)
            self.assertEqual(result['qualified'],0);mail.assert_not_called()
if __name__=='__main__':unittest.main()
