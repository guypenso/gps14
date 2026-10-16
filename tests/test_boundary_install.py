import io
import json
import importlib.util
from pathlib import Path
import pytest

spec=importlib.util.spec_from_file_location('fetch_boundary',Path(__file__).parents[1]/'scripts/fetch_boundary.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


def test_official_boundary_installer_validates_and_records_source(tmp_path):
    doc={'type':'FeatureCollection','features':[{'type':'Feature','properties':{},'geometry':{
      'type':'Polygon','coordinates':[[[-80.01,40.43],[-79.98,40.43],[-79.98,40.46],[-80.01,40.46],[-80.01,40.43]]]}}]}
    class Transport:
        def open(self,url,timeout=None):
            assert url.startswith(module.BASE)
            assert 'outSR=4326' in url
            return io.BytesIO(json.dumps(doc).encode())
    destination=tmp_path/'boundary.geojson'
    module.install(destination,Transport())
    manifest=json.loads(destination.with_suffix('.source.json').read_text())
    assert manifest['source']==module.SOURCE and len(manifest['sha256'])==64


def test_incomplete_boundary_refused(tmp_path):
    class Transport:
        def open(self,*args,**kwargs):return io.BytesIO(b'{"type":"FeatureCollection","features":[],"exceededTransferLimit":true}')
    with pytest.raises(ValueError):module.install(tmp_path/'bad.geojson',Transport())
