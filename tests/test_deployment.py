from pathlib import Path
import yaml


def test_render_has_two_slots_and_preserves_runtime_owned_financial_settings():
    path=Path(__file__).parents[1]/'render.yaml'
    config=yaml.safe_load(path.read_text())
    services={s['type']:s for s in config['services']}
    assert set(services)=={'web','cron'}
    assert services['web']['healthCheckPath']=='/healthz'
    assert services['cron']['schedule']=='0 5,6,17,18 * * *'
    assert services['cron']['startCommand']=='python -m rentalsourcing.cloud --scheduled'
    assert config['databases'][0]['ipAllowList']==[]
    common={v['key']:v.get('value') for v in config['envVarGroups'][0]['envVars']}
    runtime={v['key']:v for v in config['envVarGroups'][0]['envVars']}
    for key in ('APPROVED_API_REQUESTS','API_APPROVAL_MODE','MAX_API_REQUESTS_PER_DAY','MAX_API_REQUESTS_PER_MONTH','BILLING_DAY'):
        assert runtime[key].get('sync') is False and 'value' not in runtime[key]
    assert not common.get('API_APPROVAL_ID')
    cron={v.get('key'):v for v in services['cron']['envVars']}
    web={v.get('key'):v for v in services['web']['envVars']}
    assert cron['ENABLE_RENTCAST']['sync'] is False and cron['ENABLE_EMAIL']['sync'] is False
    assert 'value' not in cron['ENABLE_RENTCAST'] and 'value' not in cron['ENABLE_EMAIL']
    assert web['DASHBOARD_PASSWORD']['generateValue'] is True
    assert 'RENTCAST_API_KEY' not in web and 'RESEND_API_KEY' not in web
    # Blueprint updates cannot silently activate or reset provider approvals.
    assert all(k not in cron for k in ('RENTCAST_API_KEY','RESEND_API_KEY','EMAIL_FROM','EMAIL_TO'))
    assert config['previews']['generation']=='off'
    assert config['databases'][0]['postgresMajorVersion']=='17'
    assert config['databases'][0]['storageAutoscalingEnabled'] is False
    assert (path.parent/'.python-version').read_text().strip()==common['PYTHON_VERSION']
    assert common['LISTING_LIMIT']=='500'
    for service in services.values():
        assert service['branch']=='master'
        assert service['autoDeployTrigger']=='off'
        assert 'fetch_boundary.py' not in service['buildCommand']


def test_git_example_does_not_include_personal_recipient():
    text=(Path(__file__).parents[1]/'.env.example').read_text()
    assert 'EMAIL_TO=\n' in text
    assert 'gmail.com' not in text
