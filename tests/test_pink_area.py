"""Real City/WPRDC geometry; external traffic is forbidden by conftest."""
import datetime as dt
import hashlib
import json
from pathlib import Path
import pytest
from shapely.geometry import Point, shape
from conftest import listing, Provider
from rentalsourcing.geography import load_boundary, POLICY
from rentalsourcing.pink_area import NAMES, SNAPSHOT_SHA256
from rentalsourcing.core import verification, report_eligible
from rentalsourcing.criteria import reconcile_geography

ROOT=Path(__file__).resolve().parents[1]
@pytest.fixture
def policy():return load_boundary(ROOT/'data/pittsburgh.geojson')

def row_at(region,identity='fixture'):
    p=region.geometry.representative_point()
    return listing(id=identity,longitude=p.x,latitude=p.y,zipCode='15208',neighborhood='Unrelated label')

@pytest.mark.parametrize('slug',list(NAMES))
def test_confirmed_inside_excluded_in_shared_engine(policy,slug):
    row=row_at(policy.pink_regions[slug],slug)
    assert policy.evaluate(row)==('Rejected',slug.replace('-','_')+'_neighborhood')
    assert verification(row,policy)[0]=='Rejected'
    assert not report_eligible(row,policy)


def test_snapshot_matches_independently_downloaded_official_geometry():
    raw=(ROOT/'data/pittsburgh-neighborhoods-wprdc.geojson').read_bytes()
    assert hashlib.sha256(raw).hexdigest()==SNAPSHOT_SHA256
    doc=json.loads(raw);manifest=json.loads((ROOT/'data/pittsburgh-neighborhoods-wprdc.source.json').read_text())
    assert len(doc['features'])==90
    for comparison in manifest['verification']:
        name=comparison['neighborhood'];slug=name.lower().replace(' ','-')
        raw=(ROOT/'data'/(slug+'.geojson')).read_bytes()
        assert hashlib.sha256(raw).hexdigest()==comparison['official_asset_sha256']
        official=shape(json.loads(raw)['features'][0]['geometry'])
        mirror=shape(next(f for f in doc['features'] if f['properties']['hood']==name)['geometry'])
        assert official.hausdorff_distance(mirror)<1e-10


def test_north_bank_llb_and_neighboring_areas_not_rejected(policy):
    doc=json.loads((ROOT/'data/pittsburgh-neighborhoods-wprdc.geojson').read_text())
    north=shape(next(f for f in doc['features'] if f['properties']['hood']=='Lincoln-Lemington-Belmar')['geometry']).geoms[1].representative_point()
    for point in [north]+[shape(f['geometry']).representative_point() for f in doc['features'] if f['properties']['hood'] in ['Larimer','Point Breeze North','Highland Park']]:
        p=listing(longitude=point.x,latitude=point.y,zipCode='15208',description='Near Homewood and East Hills',neighborhood='Homewood North')
        assert policy.evaluate(p)==('Candidate',None)


def test_immediate_inside_outside_and_uncertain_outer_edges(policy):
    geometry=policy.pink_geometry
    tested=0
    for part in geometry.geoms if geometry.geom_type=='MultiPolygon' else [geometry]:
        ring=list(part.exterior.coords)
        for a,b in zip(ring,ring[1:]):
            dx,dy=b[0]-a[0],b[1]-a[1];length=(dx*dx+dy*dy)**.5
            if length<1e-5:continue
            x,y=(a[0]+b[0])/2,(a[1]+b[1])/2
            edge=listing(longitude=x,latitude=y,zipCode='15208')
            assert policy.evaluate(edge)==('Pending Verification','pink_area_boundary_verification')
            for side in [-1,1]:
                px,py=x+side*dy/length*1e-6,y-side*dx/length*1e-6
                p=listing(longitude=px,latitude=py,zipCode='15208')
                state,reason=policy.evaluate(p)
                assert (reason in {s.replace('-','_')+'_neighborhood' for s in NAMES})==geometry.contains(Point(px,py))
            tested+=1
            if tested>=15:return
    assert tested>=1


def test_stored_reconciliation_preserves_human_choices_source_and_history(store,policy,settings,clock):
    from rentalsourcing.engine import Engine
    from rentalsourcing.sync_runtime import get
    engine=Engine(store,policy,Provider([]),settings,clock)
    for slug,region in policy.pink_regions.items():
        p=row_at(region,slug);engine.ingest('fixture',p)
        store.set_verification(slug,'Qualified',[])
        store.save_setting('penten_review:'+slug,{'status':'monitor','reviewed_at':clock().isoformat(),'history':'preserve'})
    before=store.execute('SELECT id,payload FROM properties ORDER BY id').fetchall()
    history=store.execute('SELECT * FROM price_history ORDER BY id').fetchall()
    decisions=store.execute("SELECT * FROM settings WHERE name LIKE ? ORDER BY name",('penten_review:%',)).fetchall()
    result=reconcile_geography(store,policy,clock())
    assert result['newly_excluded']==5 and result['managed_preserved']==5 and result['provider_calls']==0
    assert reconcile_geography(store,policy,clock())==result
    assert store.execute('SELECT id,payload FROM properties ORDER BY id').fetchall()==before
    assert store.execute('SELECT * FROM price_history ORDER BY id').fetchall()==history
    assert store.execute("SELECT * FROM settings WHERE name LIKE ? ORDER BY name",('penten_review:%',)).fetchall()==decisions
    assert all(store.get_property(s)['state']=='Rejected' for s in NAMES)


def test_discovery_excludes_before_paid_tax_calls_and_reports(store,policy,settings,clock):
    from dataclasses import replace
    from rentalsourcing.collector import Client
    from rentalsourcing.engine import Engine
    from rentalsourcing.digest import Digest
    rows=[row_at(r,s) for s,r in policy.pink_regions.items()]
    provider=Provider(rows);config=replace(settings,page_size=100)
    client=Client(store,config,clock,provider,lambda _:None)
    stats=Engine(store,policy,client,config,clock).run('synthetic-pink',clock().date().isoformat())
    assert provider.tax_calls==0
    outbox=Digest(store,policy,config,clock,provider).prepare('synthetic-pink',clock().date().isoformat(),stats)
    report=json.loads(store.execute('SELECT payload FROM command_center_reports WHERE outbox_id=?',(outbox,)).fetchone()[0])
    assert report['opportunities']==[] and not provider.messages
