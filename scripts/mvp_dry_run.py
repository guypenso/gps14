"""Listing-only offline demonstration. Synthetic data; no API calls or email."""
import argparse,datetime as dt,json,sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from rentalsourcing.storage import Store
from rentalsourcing.geography import load_boundary
from rentalsourcing.discovery.models import Source,normalize
from rentalsourcing.discovery.repository import ingest,health
from rentalsourcing.discovery.report import snapshot,render


def generate(output):
    with patch('urllib.request.OpenerDirector.open',side_effect=AssertionError('Network prohibited')):
        store=Store('sqlite:///:memory:');store.migrate()
        try:
            now=dt.datetime(2026,10,24,12,tzinfo=dt.timezone.utc);before=now-dt.timedelta(hours=15)
            source=Source('fictional_mls','mls','FICTIONAL-NOT-LICENSED','synthetic sample',True,True,True)
            for i,(street,price,tax) in enumerate([('101 Fixture St',175000,1500),('202 Sample Ave',205000,1800),('303 Example Rd',160000,None)],1):
                stamp=before if i==2 else now
                row=dict(ListingKey=str(i),UnparsedAddress=street+', Pittsburgh, PA 15212',ListPrice=price,BedroomsTotal=3,BathroomsTotalInteger=1,LivingArea=1200,StandardStatus='Active',Latitude=40.45,Longitude=-80,TaxAnnualAmount=tax,TaxYear=2025,ModificationTimestamp=stamp.isoformat(),ListingURL='https://listing.example.invalid/mvp-'+str(i))
                item=normalize(row,source,observed_at=stamp.isoformat());ingest(store,item,stamp,authorized=True)
                if i==2:
                    row.update(ListPrice=195000,ModificationTimestamp=now.isoformat())
                    ingest(store,normalize(row,source,observed_at=now.isoformat()),now,authorized=True)
            health(store,source.name,now,success=True)
            data=snapshot(store,load_boundary('data/pittsburgh.geojson'),now,report_mode='mvp')
            data.update(dry_run=True,real_api_requests=0,real_emails=0,source_results=[dict(source=source.name,fresh_scan=True,records=3,complete=True)])
            body=render(data);output.mkdir(parents=True,exist_ok=True)
            for ext,value in [('html',body['html']),('txt',body['text']),('json',json.dumps(data,indent=2))]:(output/('report.'+ext)).write_text(value)
            print(json.dumps(data['counts']));return data
        finally:store.close()


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);generate(p.parse_args().output)
