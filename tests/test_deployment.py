from pathlib import Path
import yaml


def test_render_has_web_cron_postgres_and_safe_defaults():
    path=Path(__file__).parents[1]/'render.yaml'
    config=yaml.safe_load(path.read_text())
    services={s['type']:s for s in config['services']}
    assert set(services)=={'web','cron'}
    assert services['web']['healthCheckPath']=='/healthz'
    assert services['cron']['schedule']=='0 12,13 * * *'
    assert config['databases'][0]['ipAllowList']==[]
    common={v['key']:v.get('value') for v in config['envVarGroups'][0]['envVars']}
    assert common['APPROVED_API_REQUESTS']=='0' and common['API_APPROVAL_ID']==''
    cron={v.get('key'):v for v in services['cron']['envVars']}
    web={v.get('key'):v for v in services['web']['envVars']}
    assert cron['ENABLE_RENTCAST']['value']=='false' and cron['ENABLE_EMAIL']['value']=='false'
    assert web['DASHBOARD_PASSWORD']['generateValue'] is True
    assert 'RENTCAST_API_KEY' not in web and 'RESEND_API_KEY' not in web
    assert 'sync' in cron['RENTCAST_API_KEY']
    assert common['LISTING_LIMIT']=='500'
    for service in services.values():
        assert service['branch']=='master'
        assert service['autoDeployTrigger']=='checksPass'
        assert 'fetch_boundary.py' not in service['buildCommand']


def test_git_example_does_not_include_personal_recipient():
    text=(Path(__file__).parents[1]/'.env.example').read_text()
    assert 'EMAIL_TO=\n' in text
    assert 'gmail.com' not in text
