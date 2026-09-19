from copy import deepcopy
from types import SimpleNamespace

import pandas as pd
import pytest

from qmag.config import StrategyConfig
from qmag.learning import review, update_shadows
from qmag.outcome_model import train
from qmag.shadow_quality import geometry_error, quarantine_invalid
from qmag.trader import TraderState, record_shadow


def shadow(**changes):
    return dict({'symbol': 'ABC', 'kind': 'rejected', 'date': '2026-09-17', 'setup': 'breakout',
                 'entry': 100., 'stop': 95., 'target': 110., 'pivot': 100., 'immediate': True,
                 'status': 'open', 'features': {'rvol': 2.}, 'reasons': ['market_regime'],
                 'failed_checks': ['market_regime']}, **changes)


@pytest.mark.parametrize('changes', [
    {'stop': 100.}, {'stop': 101.}, {'stop': 0}, {'entry': float('nan')},
    {'stop': float('inf')}, {'target': 99.}, {'target': float('nan')}, {'entry': None},
])
def test_invalid_geometry_cannot_become_a_training_label(changes):
    row = shadow(status='closed', resolved_on='2026-09-18', r_multiple=99999., **changes)
    assert geometry_error(row)
    assert train([], [row])['records'] == 0


def test_bad_closed_and_open_shadows_are_quarantined_without_erasing_history(tmp_path):
    bad = shadow(stop=101., status='closed', resolved_on='2026-09-18', r_multiple=-360500000., mfe_r=720000000.)
    state = TraderState(shadow=[bad, shadow(symbol='OPEN', stop=100.), shadow(symbol='GOOD')])
    original = deepcopy(bad)
    assert update_shadows(state, {}, StrategyConfig(), '2026-09-18') == 0
    assert bad['status'] == 'invalid' and 'r_multiple' not in bad
    assert bad['invalid_result']['r_multiple'] == original['r_multiple']
    assert quarantine_invalid(state.shadow, '2026-09-19') == 0
    report = review(state, StrategyConfig(), tmp_path, apply=False, ai=False)
    assert report['shadows']['by_status'] == {'invalid': 2, 'open': 1}
    assert report['objective']['filtered_n'] == 0
    assert report['adjustments'] == []

    from fastapi.testclient import TestClient
    from qmag.dashboard import create_app
    from qmag.session import TradingSession, SessionSettings
    state.save(tmp_path / 'trader.json')
    session = TradingSession(SessionSettings(state_dir=tmp_path, data='csv'))
    page = TestClient(create_app(session)).get('/learning')
    assert page.status_code == 200 and 'excluded from learning' in page.text
    assert '-360500000' not in page.text


def test_valid_shadow_still_resolves_and_trains():
    state = TraderState(shadow=[shadow()])
    bars = pd.DataFrame({'open': [101.], 'high': [111.], 'low': [99.], 'close': [110.], 'volume': [1000.]}, index=pd.to_datetime(['2026-09-18']))
    assert update_shadows(state, {'ABC': bars}, StrategyConfig(), '2026-09-18') == 1
    row = state.shadow[0]
    assert row['status'] == 'closed' and row['r_multiple'] == 2.
    assert train([], [row])['records'] == 1


@pytest.mark.parametrize('entry,stop,target', [(100., 101., 98.), (100., 100., None), (100., 95., 99.), (100.00001,100.,101.)])
def test_invalid_plans_are_not_recorded_as_shadows(entry, stop, target):
    state = TraderState()
    sig = SimpleNamespace(symbol='ABC', setup='breakout', pivot=100.)
    plan = SimpleNamespace(entry=entry, stop=stop, partial_target=target, failed_checks=['stop_distance_ok'])
    assert not record_shadow(state, StrategyConfig(), 'rejected', sig, plan, '2026-09-18', ['stop_distance_ok'], {}, True)
    assert state.shadow == []
