"""Read only the existing profiles; never mutate accounts or runtime state."""
import json
import math
import shutil
from pathlib import Path

import pytest
import yaml

from src.astock.trade.execution_policy import load_execution_policies

ROOT=Path(__file__).resolve().parents[1]


@pytest.fixture
def config_project(tmp_path):
    root=tmp_path/'project'
    shutil.copytree(ROOT/'configs',root/'configs')
    return root


def edit_yaml(path,mutate):
    data=yaml.safe_load(path.read_text());mutate(data)
    path.write_text(yaml.safe_dump(data,allow_unicode=True,sort_keys=False))


def test_existing_profiles_retain_distinct_execution_and_risk_rules():
    paths=[ROOT/'configs/accounts.yaml',ROOT/'configs/strategies/balanced.yaml',ROOT/'configs/strategies/aggressive.yaml']
    before={p:p.read_bytes() for p in paths}
    profiles=load_execution_policies(ROOT)
    for account,mult,floor,days,warn,freeze in [
            ('balanced',3.5,True,1,8.,12.),('aggressive',4.5,False,2,15.,20.)]:
        p=profiles[account]
        assert p['account_id']==account
        assert (p['rebalance_every'],p['min_holding_days'],p['cooldown_days'])==(1,5,3)
        assert p['refill_vacancies'] is True
        assert p['stop_loss_pct']==-6. and p['stop_loss_mode']=='intraday'
        assert p['trailing_stop_enabled'] is True and p['trailing_stop_mode']=='atr'
        assert p['trailing_stop_activation_pct']==20. and p['atr_trailing_multiplier']==mult
        assert p['profit_floor_enabled'] is floor and p['profit_floor_pct']==3.
        assert p['realtime_ma20_exit'] is True
        assert p['ma20_exit_consecutive_days']==days and p['ma20_exit_buffer_pct']==1.
        assert (p['max_drawdown_warn_pct'],p['max_drawdown_freeze_pct'])==(warn,freeze)
        assert (p['max_entry_gap_down_pct'],p['max_entry_gap_up_pct'],p['max_entry_intraday_drop_pct'])==(3.,5.,3.)
        assert p['entry_require_ma20'] is p['protect_limit_up'] is True
        assert p['sources']['primary_overrides']==[]
        assert p['sources']['fields']['trailing_stop_activation_pct'].startswith('legacy_rule:')
        assert p['sources']['fields']['max_drawdown_freeze_pct'].endswith(f'accounts.{account}.risk_limits.max_drawdown_freeze_pct')
    assert {p:p.read_bytes() for p in paths}==before
    json.dumps(profiles,allow_nan=False)  # Metadata and values are audit/API serializable.


def test_primary_only_overrides_known_balanced_policy_keys():
    base=load_execution_policies(ROOT)
    primary={'atr_trailing_multiplier':3.8,'trailing_stop_activation_pct':22.,
             'max_entry_gap_up_pct':4.,'min_holding_days':6,
             'max_positions':99,'max_drawdown_freeze_pct':99.,'other_dictionary':{'unrelated':'ignored'}}
    out=load_execution_policies(ROOT,primary)
    assert out['balanced']['atr_trailing_multiplier']==3.8
    assert out['balanced']['trailing_stop_activation_pct']==22.
    assert out['balanced']['max_entry_gap_up_pct']==4.
    assert out['balanced']['min_holding_days']==6
    assert out['balanced']['max_drawdown_freeze_pct']==12.
    assert 'max_positions' not in out['balanced']
    assert 'other_dictionary' not in out['balanced']
    assert out['aggressive']==base['aggressive']
    assert out['balanced']['sources']['fields']['atr_trailing_multiplier']=='primary_parameters.atr_trailing_multiplier'
    assert set(out['balanced']['sources']['primary_overrides'])=={'atr_trailing_multiplier','trailing_stop_activation_pct','max_entry_gap_up_pct','min_holding_days'}


@pytest.mark.parametrize('relative',['configs/accounts.yaml','configs/strategies/balanced.yaml','configs/strategies/aggressive.yaml'])
def test_missing_required_file_raises_even_with_primary_values(config_project,relative):
    (config_project/relative).unlink()
    with pytest.raises(FileNotFoundError):load_execution_policies(config_project,{'rebalance_every':5})


def test_missing_nonlegacy_parameter_never_uses_invented_default(config_project):
    edit_yaml(config_project/'configs/strategies/balanced.yaml',lambda d:d['parameters'].pop('stop_loss_pct'))
    with pytest.raises(ValueError,match='Missing balanced execution parameter: stop_loss_pct'):
        load_execution_policies(config_project)


@pytest.mark.parametrize('key,value',[
    ('stop_loss_pct',float('nan')),('atr_trailing_multiplier',float('inf')),
    ('rebalance_every',0),('rebalance_every',True),('cooldown_days',-1),
    ('trailing_stop_enabled','true'),('stop_loss_pct',0.),('risk_per_trade',True),
    ('ma20_exit_consecutive_days',0),('max_entry_gap_up_pct',-1.),
    ('max_entry_intraday_drop_pct',float('-inf')),('entry_require_ma20',1),
    ('trailing_stop_mode','unknown'),('stop_loss_mode','unknown'),
    ('refill_vacancies','true'),('refill_vacancies',1),('participation_rate',0.),
])
def test_illegal_primary_parameter_is_rejected(key,value):
    with pytest.raises(ValueError):load_execution_policies(ROOT,{key:value})


@pytest.mark.parametrize('account',['balanced','aggressive'])
def test_invalid_profile_number_is_not_masked_by_account_defaults(config_project,account):
    edit_yaml(config_project/f'configs/strategies/{account}.yaml',lambda d:d['parameters'].update(atr_trailing_multiplier=float('nan')))
    with pytest.raises(ValueError,match=f'{account}.atr_trailing_multiplier'):
        load_execution_policies(config_project)


def test_account_drawdown_limits_are_authoritative_and_validated(config_project):
    edit_yaml(config_project/'configs/accounts.yaml',lambda d:d['accounts']['aggressive']['risk_limits'].update(max_drawdown_warn_pct=22.,max_drawdown_freeze_pct=20.))
    with pytest.raises(ValueError,match='warning must be below freeze'):
        load_execution_policies(config_project)


def test_account_profile_identity_mismatch_is_rejected(config_project):
    edit_yaml(config_project/'configs/strategies/aggressive.yaml',lambda d:d.update(strategy_id='wrong-profile'))
    with pytest.raises(ValueError,match='strategy identity'):
        load_execution_policies(config_project)


def test_unrelated_bad_primary_value_is_ignored_not_broadcast_to_aggressive():
    profiles=load_execution_policies(ROOT,{'unrelated':float('nan'),'strategy_state':{'x':object()}})
    assert profiles['balanced']['atr_trailing_multiplier']==3.5
    assert profiles['aggressive']['atr_trailing_multiplier']==4.5


def test_primary_can_toggle_refill_vacancies_and_daily_rebalance():
    out=load_execution_policies(ROOT,{'rebalance_every':1,'refill_vacancies':False})
    assert out['balanced']['rebalance_every']==1
    assert out['balanced']['refill_vacancies'] is False
    assert out['aggressive']['refill_vacancies'] is True
