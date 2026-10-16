from dataclasses import replace
import json
import pytest
from rentalsourcing.cloud import main


def test_default_entrypoint_is_inert(monkeypatch):
    monkeypatch.setenv('ENABLE_RENTCAST','false')
    monkeypatch.delenv('DATABASE_URL',raising=False)
    main([])


def test_missing_production_geometry_fails_preflight_offline(monkeypatch,tmp_path):
    monkeypatch.setenv('CITY_BOUNDARY_FILE',str(tmp_path/'absent.geojson'))
    with pytest.raises(ValueError):main(['--check'])


def test_production_requires_postgres(monkeypatch):
    monkeypatch.setenv('ENABLE_RENTCAST','true');monkeypatch.setenv('DATABASE_URL','sqlite:///:memory:')
    with pytest.raises(ValueError,match='PostgreSQL'):main(['--migrate'])


def test_manifest_checksum_is_enforced(tmp_path):
    from rentalsourcing.geography import load_boundary,SOURCE,DOWNLOAD_PREFIX
    p=tmp_path/'boundary.geojson';p.write_text('{}')
    p.with_suffix('.source.json').write_text(json.dumps(dict(source=SOURCE,download_url=DOWNLOAD_PREFIX,sha256='wrong')))
    with pytest.raises(ValueError,match='checksum'):load_boundary(p)


def test_no_secret_in_structured_logs(monkeypatch,caplog):
    from rentalsourcing.telemetry import event
    import logging
    monkeypatch.setenv('RENTCAST_API_KEY','fixture-secret')
    with caplog.at_level(logging.INFO):event('test',reason='hidden fixture-secret')
    assert 'fixture-secret' not in caplog.text and '[REDACTED]' in caplog.text
