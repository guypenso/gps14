"""Generate synthetic dual-strategy email with NO network, providers or delivery."""
import argparse,datetime as dt,json,os,sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from rentalsourcing.storage import Store
from rentalsourcing.geography import load_boundary
from rentalsourcing.discovery.models import Source,normalize
from rentalsourcing.discovery.repository import ingest
from rentalsourcing.discovery.finance import Input
from rentalsourcing.discovery.config import DiscoverySettings
from rentalsourcing.discovery.report import snapshot,render


def hypothetical(value):return Input(value,'hypothetical','FICTIONAL REVIEW SCENARIO; NOT APPROVED')


def generate(output):
    def forbidden(*args,**kwargs):raise AssertionError('Network/provider traffic prohibited')
    with patch('urllib.request.OpenerDirector.open',forbidden):
        store=Store('sqlite:///:memory:');store.migrate()
        try:
            now=dt.datetime(2026,10,24,12,tzinfo=dt.timezone.utc)
            source=Source('fictional_mls','mls','FICTIONAL-NOT-A-LICENSE','fictional Pittsburgh modeling cohort',True,True,True)
            common={name:hypothetical(value) for name,value in dict(vacancy_rate=.05,maintenance_rate=.08,capex_rate=.05,management_rate=.10,closing_cost_rate=.03).items()}
            loans={name:{key:hypothetical(value) for key,value in terms.items()} for name,terms in dict(base=dict(loan_to_value=.75,interest_rate=.065,loan_years=30,loan_closing_cost=3000),lower_leverage=dict(loan_to_value=.65,interest_rate=.065,loan_years=30,loan_closing_cost=3000)).items()}
            specific={}
            examples=[('101 Fixture St',140000,1500,1500,0,1200,10000),('202 Sample Ave',180000,1900,1800,50,1500,15000),('404 Fiction Rd',120000,1250,1400,0,1400,20000),('505 Example St',165000,None,1600,None,None,None)]
            for i,(street,price,rent,tax,hoa,insurance,reno) in enumerate(examples,1):
                row=dict(ListingKey='fictional-'+str(i),ListingId='FICTIONAL-'+str(i),OriginatingSystemName='FictionalMLS',UnparsedAddress=street+', Pittsburgh, PA 15212',ListPrice=price,BedroomsTotal=3,BathroomsFull=1,BathroomsHalf=0,LivingArea=1200,StandardStatus='Active',Latitude=40.45,Longitude=-80,TaxAnnualAmount=tax,TaxYear=2025,PropertySubType='SingleFamilyResidence',ModificationTimestamp=now.isoformat(),ListingURL='https://listing.example.invalid/dual-'+str(i))
                if rent is not None:row['EstimatedMonthlyRent']=rent
                if hoa is not None:row.update(AssociationFee=hoa,AssociationFeeFrequency='Monthly')
                item=normalize(row,source,observed_at=now.isoformat());ingest(store,item,now,authorized=True)
                specific[item.entity_id]={key:hypothetical(value) for key,value in dict(annual_insurance=insurance,renovation_budget=reno).items() if value is not None}
            cfg=DiscoverySettings(enabled=True,approval_id='FICTIONAL-NOT-PERMISSION',sources=({'source':source,'transport':'reso'},),assumptions=common,property_inputs=specific,financing_scenarios=loans)
            data=snapshot(store,load_boundary('data/pittsburgh.geojson'),now,assumptions=cfg.assumptions,dual_mode=True,loan_scenarios=loans,property_inputs=specific,ranking_weights=cfg.ranking_weights)
            data.update(dry_run=True,real_api_requests=0,real_emails=0)
            body=render(data);output.mkdir(parents=True,exist_ok=True)
            for suffix,value in [('json',json.dumps(data,indent=2)),('txt',body['text']),('html',body['html'])]:(output/('report.'+suffix)).write_text(value)
            print(json.dumps({'synthetic':True,'counts':data['counts'],'cash_ranked':len(data['rankings']['cash']),'financed_ranked':{k:len(v) for k,v in data['rankings']['financed'].items()},'real_api_requests':0,'real_emails':0}))
            return data
        finally:store.close()


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);generate(p.parse_args().output)
