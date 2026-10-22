"""Template regressions use redacted structure. Original .eml tested only if explicitly supplied."""
import datetime as dt
import json
import os
from pathlib import Path
from email import policy
from email.parser import BytesParser
from email.message import EmailMessage
from cryptography.fernet import Fernet
import pytest
from rentalsourcing import realscout as p
from rentalsourcing import realscout_store as rs
from rentalsourcing.realscout_report import report
from rentalsourcing.storage import Store
from rentalsourcing.geography import load_boundary

ROOT=Path(__file__).parent/'fixtures/realscout'

@pytest.mark.parametrize('kind',['html','plain'])
def test_exclusive_nested_template_full_state_and_price_before_address(kind):
    raw=(ROOT/('exclusive-'+kind+'.eml')).read_bytes()
    with pytest.raises(ValueError):p.parse_email(raw)
    r=p.parse_email(raw,profile='exclusive_alert')
    assert r.search_name is None and r.alert_profile=='exclusive_alert' and not r.issues
    assert len(r.cards)==3
    assert [c.street for c in r.cards]==['101 Fixture STREET','202 Sample STREET','303 Example Ave']
    assert [c.price for c in r.cards]==[178000,148000,68000]
    assert [c.square_footage for c in r.cards]==[925,1350,1475]
    assert [c.days_on_site for c in r.cards]==[2,9,25]
    assert all(c.state=='PA' and c.alert_kind=='exclusive' and not c.issues for c in r.cards)
    assert all('SYNTHETIC_LINK_' in c.detail_url for c in r.cards)
    assert 'SYNTHETIC_LINK_' not in json.dumps(r.public())


def test_area_number_is_not_part_of_street():
    for text,street in [('925 Sqft 909 Fiction STREET Pittsburgh, Pennsylvania, 15212','909 Fiction STREET'),
                        ('1,350 Sqft 808 Example STREET Pittsburgh, Pennsylvania, 15212','808 Example STREET')]:
        assert p.addresses(text)[0]['street']==street


def test_exclusives_profile_does_not_accept_arbitrary_search_or_subject():
    raw=(ROOT/'exclusive-html.eml').read_bytes()
    m=BytesParser(policy=policy.default).parsebytes(raw);m.replace_header('Subject','Other newsletter')
    with pytest.raises(ValueError):p.parse_email(m.as_bytes(),profile='exclusive_alert')
    with pytest.raises(ValueError):p.parse_email(raw,profile='anything')


def original():
    path=os.getenv('REALSCOUT_SAMPLE_EML')
    if not path:pytest.skip('Original authorized email is not committed; provide REALSCOUT_SAMPLE_EML offline')
    return Path(path).read_bytes()


def expected():
    path=os.getenv('REALSCOUT_SAMPLE_EXPECTED_JSON')
    if not path:pytest.skip('Private original expectations are not committed; provide REALSCOUT_SAMPLE_EXPECTED_JSON offline')
    return [tuple(row) for row in json.loads(Path(path).read_text())]


def test_authorized_original_exact_fields_and_links_without_resolving_them():
    raw=original();r=p.parse_email(raw,profile='exclusive_alert')
    assert not r.issues and len(r.cards)==3 and r.search_name is None
    assert [(c.street,c.zipcode,c.price,c.bedrooms,c.bathrooms,c.square_footage,c.days_on_site) for c in r.cards]==expected()
    m=BytesParser(policy=policy.default).parsebytes(raw);t=p.Tree();t.feed(m.get_body(preferencelist=('html',)).get_content())
    original_links=[a.attrs['href'] for a in t.links if a.text().strip().casefold()=='view details']
    assert [c.detail_url for c in r.cards]==original_links
    assert len(set(original_links))==3
    assert all(p.safe_url(url) for url in original_links)
    assert all(url not in json.dumps(r.public()) for url in original_links)


def test_authorized_original_html_and_mjml_agree():
    raw=original();m=BytesParser(policy=policy.default).parsebytes(raw)
    plain=EmailMessage();plain['Subject']=m['Subject'];plain['Date']=m['Date'];plain['Message-ID']=m['Message-ID']
    plain.set_content(m.get_body(preferencelist=('plain',)).get_content())
    a=p.parse_email(raw,profile='exclusive_alert');b=p.parse_email(plain.as_bytes(),profile='exclusive_alert')
    assert a.cards==b.cards


def test_authorized_original_database_roundtrip_duplicate_and_criteria_offline():
    r=p.parse_email(original(),profile='exclusive_alert');s=Store('sqlite:///:memory:');s.migrate();rs.migrate(s)
    now=dt.datetime(2026,10,22,12,tzinfo=dt.timezone.utc);vault=rs.LinkVault(Fernet.generate_key())
    try:
        s.execute('INSERT INTO usage VALUES (?,?)',('approval:preserve-existing',4))
        assert not rs.ingest(s,r,received_at=now.isoformat(),authorized=True,vault=vault)['duplicate_message']
        assert rs.ingest(s,r,received_at=now.isoformat(),authorized=True,vault=vault)['duplicate_message']
        assert s.execute('SELECT COUNT(*) FROM realscout_candidates').fetchone()[0]==3
        assert s.execute('SELECT COUNT(*) FROM realscout_observations').fetchone()[0]==3
        for c in r.cards:
            payload,cipher=s.execute('SELECT payload,detail_ciphertext FROM realscout_candidates WHERE id=?',(c.candidate_id,)).fetchone()
            assert vault.decrypt(cipher)==c.detail_url and c.detail_url not in payload+cipher
            stored=json.loads(payload)
            assert stored['search_name'] is None and stored['alert_profile']=='exclusive_alert'
            assert stored['days_on_site']==c.days_on_site
        boundary=load_boundary('data/pittsburgh.geojson');data=report(s,boundary,now)
        rows={row['address'].split(',')[0]:row for row in data['rows']}
        verified=expected()
        bad=rows[verified[0][0]]
        assert bad['state']=='Rejected' and {'bedrooms','squareFootage'}<=set(bad['reasons'])
        for street in [entry[0] for entry in verified[1:]]:
            row=rows[street]
            assert row['state']=='Pending Verification'
            assert {'status','coordinates','stale_email_observation'}<=set(row['reasons'])
        assert s.count_usage('approval:preserve-existing')==4
        assert s.execute('SELECT COUNT(*) FROM properties').fetchone()[0]==0
        assert s.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]==0
    finally:s.close()
