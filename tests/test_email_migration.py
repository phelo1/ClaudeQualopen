from qmag.alerts import Alerter, email_message
from qmag.settings import SettingsStore, SECRET_NAMES
from qmag.config import StrategyConfig


ENV = {'RESEND_API_KEY':'fixture-only','QMAG_ALERT_EMAIL_FROM':'desk@example.invalid','QMAG_ALERT_EMAIL_TO':'owner@example.invalid'}


def test_existing_email_configuration_survives_export_import(tmp_path):
    source=SettingsStore(tmp_path/'source');source.save_env(ENV)
    target=SettingsStore(tmp_path/'target')
    target.import_bundle(source.export_bundle(StrategyConfig(),include_secrets=True))
    assert target.load_env() == ENV
    assert 'RESEND_API_KEY' in SECRET_NAMES
    assert 'fixture-only' not in source.export_bundle(StrategyConfig(),include_secrets=False)


def test_email_defaults_to_attention_events_without_network():
    sent=[]
    alerts=Alerter(env=ENV,transport=lambda channel,payload:sent.append((channel,payload)))
    alerts.send('routine','fill',level='info',wait=True)
    assert not sent
    alerts.send('problem','connection failed',level='error',wait=True)
    assert len(sent)==1 and sent[0][0]=='email'


def test_email_routine_opt_in_and_escaped_message():
    sent=[]
    alerts=Alerter(env={**ENV,'QMAG_ALERT_EMAIL_TRADES':'yes'},transport=lambda *args:sent.append(args))
    alerts.send('fill','<unsafe>',wait=True)
    assert len(sent)==1
    body=email_message(sent[0][1],ENV)
    assert '&lt;unsafe&gt;' in body['html'] and '<unsafe>' not in body['html']
    assert body['to']==['owner@example.invalid']
