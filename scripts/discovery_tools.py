#!/usr/bin/env python3
"""Offline multi-source demo or read-only authorized benchmark of the maintained DB."""
import argparse,csv,datetime as dt,hashlib,io,json,os,sys
from pathlib import Path
from dataclasses import replace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from rentalsourcing.storage import Store
from rentalsourcing.geography import load_boundary
from rentalsourcing.discovery.models import Source,normalize
from rentalsourcing.discovery.repository import ingest,health
from rentalsourcing.discovery.connectors import CSVConnector
from rentalsourcing.discovery.finance import Input,scenarios
from rentalsourcing.discovery.report import snapshot,render
from rentalsourcing.discovery.coverage import compare,from_store
from rentalsourcing.discovery.budget import forecast


def demo(out):
    now=dt.datetime(2026,10,24,12,tzinfo=dt.timezone.utc);first=now-dt.timedelta(hours=2)
    store=Store('sqlite:///:memory:');store.migrate()
    mls=Source('synthetic_mls','mls','SYNTHETIC-ONLY','fictional residential sale cohort',True,True,True)
    rc=Source('synthetic_rentcast','rentcast','SYNTHETIC-ONLY','fictional complement',True,False,True)
    raw=Path('tests/fixtures/discovery/mls.csv').read_bytes()
    CSVConnector(mls).import_bytes(store,raw,first.isoformat(),now,authorized=True)
    rows=list(csv.DictReader(io.StringIO(raw.decode())));change=dict(rows[0],ListPrice='140000',ModificationTimestamp=(now-dt.timedelta(hours=1)).isoformat())
    item=normalize(change,mls,observed_at=(now-dt.timedelta(hours=1)).isoformat());ingest(store,item,now,authorized=True)
    rent=normalize(dict(id='fictional-rent-1',formattedAddress=change['UnparsedAddress']),rc,observed_at=now.isoformat(),style='rentcast')
    ingest(store,replace(rent,fields={'monthly_rent':1500},estimates=('monthly_rent',),comparables=({'id':'fictional-comp','formattedAddress':'909 Fiction St, Pittsburgh, PA 15212','price':1525,'distance':.4,'correlation':.9},)),now,authorized=True)
    missing=normalize(dict(id='fictional-5',formattedAddress='505 Sample St, Pittsburgh, PA 15212',price=165000,status='Active',bedrooms=3,bathrooms=1,squareFootage=1200,latitude=40.45,longitude=-80,propertyType='Single Family'),rc,observed_at=now.isoformat(),style='rentcast')
    ingest(store,replace(missing,fields={**missing.fields,'annual_tax':1600,'tax_year':2025}),now,authorized=True)
    assumption_values={'annual_insurance':1200,'vacancy_rate':.05,'maintenance_rate':.05,'capex_rate':.05,'management_rate':.08,'closing_cost':4500,'renovation_budget':10000}
    inputs={k:Input(v,'assumption','explicit fictional scenario','SYNTHETIC-NOT-OWNER-POLICY') for k,v in assumption_values.items()}
    boundary=load_boundary('data/pittsburgh.geojson');data=snapshot(store,boundary,now,assumptions=inputs,financing='cash')
    data['dry_run']=True;body=render(data);out.mkdir(parents=True,exist_ok=True)
    (out/'report.json').write_text(json.dumps(data,indent=2));(out/'report.txt').write_text(body['text']);(out/'report.html').write_text(body['html'])
    observations=[]
    for source,key,processed,oid in store.execute('SELECT source,entity_id,processed_at,id FROM discovery_observations').fetchall():observations.append(dict(observation_id=oid,source=source,key=key,discovered_at=processed,status='Active'))
    entities={p['address'].split(',')[0]:p['id'] for p in data['properties']}
    reference=[dict(key=key,relevant=street!='303 Example St',listed_at=first.isoformat(),status='Active',zipcode='15212') for street,key in entities.items()]
    coverage=compare(observations,reference=reference,reference_permission='FICTIONAL-COHORT-ONLY',as_of=now.isoformat())
    coverage['synthetic']=True;(out/'synthetic-coverage.json').write_text(json.dumps(coverage,indent=2))
    (out/'request-forecast.json').write_text(json.dumps({'assumptions':{'days':31,'pages_per_scan':1,'uncached_candidates':9,'requests_per_candidate':2,'verification':6},'forecasts':forecast()},indent=2))
    print(json.dumps(dict(synthetic=True,provider_requests=0,emails_sent=0,counts=data['counts'],real_mls_coverage=None)))
    store.close()


def main():
    p=argparse.ArgumentParser(description=__doc__);g=p.add_mutually_exclusive_group(required=True)
    g.add_argument('--synthetic',action='store_true');g.add_argument('--benchmark',type=Path)
    p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    if a.synthetic:demo(a.output);return
    manifest=json.loads(a.benchmark.read_text())
    if not manifest.get('permission_id') or not manifest.get('scope') or not manifest.get('as_of'):raise PermissionError('Authorized benchmark manifest with snapshot time required')
    store=Store(os.environ['DATABASE_URL'],initialize=False)
    try:
        if store.pg:store.execute('SET default_transaction_read_only = on')
        result=from_store(store,dt.datetime.now(dt.timezone.utc),reference=manifest['listings'],reference_permission=manifest['permission_id'],as_of=manifest['as_of'])
        result['scope']=manifest['scope'];a.output.write_text(json.dumps(result,indent=2))
    finally:store.close()


if __name__=='__main__':main()
