"""Stored-only official assessment CSV import; never provider calls or emails."""
import argparse
import datetime as dt
import json
import os
from .config import Settings
from .core import verification
from .database import validate_postgres_url
from .geography import load_boundary
from .public_tax import import_local_csv
from .storage import Store


def run(store,boundary,csv_path,now,execute=False):
    rows=[]
    for raw,source in store.execute('SELECT payload,source FROM properties').fetchall():
        if source!='rentcast-live':continue
        item=json.loads(raw)
        if verification(item,boundary)[0]=='Candidate':rows.append(item)
    estimates,manifest=import_local_csv(csv_path,rows,boundary,now)
    counts={'candidates_checked':len(rows),'unique_parcel':sum(x.get('match_status')=='unique_parcel' for x in estimates.values()),'estimated':sum(x.get('state')=='estimated' for x in estimates.values()),'unknown':sum(x.get('state')=='unknown' for x in estimates.values()),'provider_calls':0,'verified_bills_created':0}
    if execute:
        if not store.lock():raise ValueError('Another application job is running')
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
