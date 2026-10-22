#!/usr/bin/env python3
"""Offline report through maintained application modules; no network or persistent DB.

Use --eml for an explicitly supplied sample OR --synthetic for a labeled demo.
Private source links are encrypted with an ephemeral key and never exported.
"""
import argparse
import datetime as dt
from email.message import EmailMessage
from email.utils import format_datetime
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cryptography.fernet import Fernet
from rentalsourcing.geography import load_boundary
from rentalsourcing.realscout import parse_email,SEARCH
from rentalsourcing.realscout_store import LinkVault,ingest
from rentalsourcing.realscout_report import report,render_preview
from rentalsourcing.storage import Store


def synthetic(store,vault,now):
    def message(stamp,changed=False):
        m=EmailMessage();m['Subject']=SEARCH;m['Date']=format_datetime(stamp)
        m['Message-ID']='<synthetic-'+str(int(stamp.timestamp()))+'@example.invalid>'
        cards=[('101 Fixture St',170000 if changed else 180000,3,1200)]
        if not changed:cards += [('202 Sample Ave',155000,3,1400),('303 Example St',120000,2,950)]
        m.set_content(SEARCH+'\n'+'\n'.join(
            f'{street}, Pittsburgh, PA 15212\n${price:,}\n{beds} Beds | 1 Bath | {area} Sq Ft\nListing status: Active\nView Details: https://client.realscout.com/private/SYNTHETIC-{i}'
            for i,(street,price,beds,area) in enumerate(cards)))
        return parse_email(m.as_bytes())
    first=message(now-dt.timedelta(hours=2));second=message(now-dt.timedelta(hours=1),True)
    for parsed in (first,second):ingest(store,parsed,received_at=now.isoformat(),processed_at=now.isoformat(),authorized=True,vault=vault)
    # Synthetic provider evidence exercises the same financial policy; no provider is called.
    c=first.cards[0];record=dict(id='SYNTHETIC-PROPERTY',formattedAddress='101 Fixture St, Pittsburgh, PA 15212',
        latitude=40.45,longitude=-80,propertyTaxes={'2025':{'year':2025,'total':1000}})
    store.execute('INSERT INTO realscout_evidence VALUES (?,?,?,?,?,?)',(c.candidate_id,record['id'],json.dumps(record),
        'rentcast-live',now.isoformat(),(now+dt.timedelta(days=7)).isoformat()))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    g=p.add_mutually_exclusive_group(required=True);g.add_argument('--eml',type=Path);g.add_argument('--synthetic',action='store_true')
    p.add_argument('--profile',choices=['exclusive_alert','saved_search'],default='exclusive_alert')
    p.add_argument('--as-of',default='2026-10-22T12:00:00+00:00')
    p.add_argument('--output-dir',type=Path,required=True)
    a=p.parse_args();now=dt.datetime.fromisoformat(a.as_of)
    if now.tzinfo is None:p.error('--as-of must include timezone')
    store=Store('sqlite:///:memory:');store.migrate();vault=LinkVault(Fernet.generate_key())
    try:
        if a.synthetic:synthetic(store,vault,now)
        else:ingest(store,parse_email(a.eml.read_bytes(),profile=a.profile),received_at=now.isoformat(),
                    processed_at=now.isoformat(),authorized=True,vault=vault)
        data=report(store,load_boundary('data/pittsburgh.geojson'),now,public_url='https://dashboard.example.invalid')
        data['dry_run']=True;data['synthetic']=a.synthetic
        body=render_preview(data)
        label='SYNTHETIC TEST DATA — NOT REAL INVESTMENTS' if a.synthetic else 'AUTHORIZED SUPPLIED SAMPLE — OFFLINE ONLY'
        body['text']=label+'\n'+body['text'];body['html']=body['html'].replace('<body>','<body><strong>'+label+'</strong>')
        a.output_dir.mkdir(parents=True,exist_ok=True)
        for name,value in [('report.json',json.dumps(data,ensure_ascii=False,indent=2)),('report.txt',body['text']),('report.html',body['html'])]:
            (a.output_dir/name).write_text(value,encoding='utf-8')
        print(json.dumps(dict(offline=True,provider_requests=0,emails_sent=0,counts=data['counts'])))
    finally:store.close()


if __name__=='__main__':main()
