import datetime as dt
import json
import math
from pathlib import Path

from rentalsourcing.geography import load_boundary
from rentalsourcing.request import municipal_queries,listing_parameters
from rentalsourcing.partition_discovery import _state,PREFIX


def test_live_query_contract_uses_verified_municipal_bounds_not_postal_labels():
    boundary=load_boundary(Path(__file__).parents[1]/'data/pittsburgh.geojson')
    queries=dict(municipal_queries(boundary))
    assert set(queries)=={'bethel-park','pleasant-hills'}
    for slug,name in [('bethel-park','Bethel Park Municipality'),('pleasant-hills','Pleasant Hills Borough')]:
        q=queries[slug]
        assert 'city' not in q and 'zipCode' not in q and q['limit']==500
        assert q['bedrooms']=='2:4' and q['price']=='*:200001' and q['state']=='PA'
        west,south,east,north=boundary.additional_municipalities[name].geometry.bounds
        for x in (west,east):
            for y in (south,north):
                a,b=map(math.radians,(q['latitude'],y));dy=b-a;dx=math.radians(x-q['longitude'])
                distance=3958.7613*2*math.asin(math.sqrt(math.sin(dy/2)**2+math.cos(a)*math.cos(b)*math.sin(dx/2)**2))
                assert distance<=q['radius']
    assert 'city' not in listing_parameters(boundary)
    for point in [(-80.045,40.38),(-79.90,40.36),(-80.05,40.20),(-80.3,40.7),(-79.6,40.3)]:
        assert boundary.evaluate({'longitude':point[0],'latitude':point[1],'zipCode':'15102'})[0]=='Rejected'


def test_query_transition_archives_legacy_cursor_and_preserves_rotation_accounting(store,clock):
    old=_state(store,'bethel-park',{'city':'Bethel Park','zipCode':'15102'})
    old.update(job_id='existing-cursor',offset=4,query_complete=False,selection_cycle='approved-cycle',selection_pages=3)
    store.job('existing-cursor','2026-11-16');store.checkpoint('existing-cursor',4,False,{},'partial')
    store.save_setting(PREFIX+'bethel-park',old)
    new=_state(store,'bethel-park',{'latitude':40.3,'longitude':-80.0,'radius':3})
    archived=json.loads(store.execute('SELECT payload FROM settings WHERE name=?',(new['previous_query']['query_history_key'],)).fetchone()[0])
    assert archived==old
    assert new['offset']==0 and new['job_id'] is None and new['selection_pages']==3
    assert store.execute('SELECT offset_value FROM scan_jobs WHERE id=?',('existing-cursor',)).fetchone()[0]==4
    assert store.execute('SELECT COUNT(*) FROM usage').fetchone()[0]==0
