"""Stored-only official assessment CSV import; never provider calls or emails."""
import argparse
import datetime as dt
import json
import os
from .config import Settings
from .core import verification
from .database import validate_postgres_url
from .geography import load_boundary
from .public_tax import import_local_csv,unknown
from .storage import Store


def preserve_evidence(estimates,old_estimates,source,now):
    retained=conflicts=0
    for identity,current in estimates.items():
        prior=old_estimates.get(identity)
        if not prior or prior.get('state')!='estimated':continue
        if current.get('state')=='estimated' and current.get('parcel_id')!=prior.get('parcel_id'):
            estimates[identity]=unknown(now,'Conflicting official parcel identities across assessment batches','conflict',sources=[source])
            conflicts+=1
        elif current.get('state')!='estimated':
            estimates[identity]={**prior,'warnings':list(dict.fromkeys([*prior.get('warnings',[]),'Retained earlier assessment estimate; newer batch did not resolve this parcel.'])),'retained_from_prior_batch':True}
            retained+=1
    return retained,conflicts


def run(store,boundary,csv_path,now,execute=False):
    rows=[]
    for raw,source in store.execute('SELECT payload,source FROM properties').fetchall():
        if source!='rentcast-live':continue
        item=json.loads(raw)
        if verification(item,boundary)[0]=='Candidate':rows.append(item)
    estimates,manifest=import_local_csv(csv_path,rows,boundary,now)
    previous_row=store.execute('SELECT payload FROM settings WHERE name=?',('public_tax_batch:2026',)).fetchone()
    previous=json.loads(previous_row[0]) if previous_row else {}
    old_estimates=previous.get('estimates',{})
    retained,conflicts=preserve_evidence(estimates,old_estimates,manifest['source'],now)
    counts={'candidates_checked':len(rows),'unique_parcel':sum(x.get('match_status')=='unique_parcel' for x in estimates.values()),'estimated':sum(x.get('state')=='estimated' for x in estimates.values()),'unknown':sum(x.get('state')=='unknown' for x in estimates.values()),'retained_prior_estimates':retained,'parcel_conflicts':conflicts,'provider_calls':0,'verified_bills_created':0}
    if execute:
        if not store.lock():raise ValueError('Another application job is running')
        old_sha=previous.get('manifest',{}).get('sha256')
        if old_sha and old_sha!=manifest['sha256']:
            store.save_setting('public_tax_batch_archive:'+old_sha,previous)
        store.save_setting('public_tax_batch:2026',{'estimates':estimates,'manifest':manifest})
    return {'counts':counts,'manifest':manifest,'saved':bool(execute)}


def main():
    parser=argparse.ArgumentParser(description='Import an authorized WPRDC assessment CSV; estimates only')
    parser.add_argument('--csv',required=True);parser.add_argument('--execute',action='store_true')
    args=parser.parse_args()
    store=Store(validate_postgres_url(os.getenv('DATABASE_URL','')),initialize=False)
    try:
        result=run(store,load_boundary(Settings.from_env().boundary_file),args.csv,dt.datetime.now(dt.timezone.utc),args.execute)
        print(json.dumps(result,sort_keys=True))
    finally:store.close()

if __name__=='__main__':main()
