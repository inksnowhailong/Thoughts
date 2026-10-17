from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from guanjia.core.model_policy import ModelPolicy, ModelProfile


def test_model_selection_covers_each_role_without_business_model_names() -> None:
    policy = ModelPolicy((
        ModelProfile('fast-free', roles=('main', 'worker', 'memory'), capabilities=('text',), cost=3, free_tokens=100),
        ModelProfile('vision-low', roles=('vision',), capabilities=('vision',), cost=1),
        ModelProfile('general-paid', roles=('main', 'worker', 'vision', 'memory'), capabilities=('text', 'vision'), cost=2),
    ))

    assert policy.select('main', estimated_tokens=1).name == 'fast-free'
    assert policy.select('worker', required_capabilities=('text',), estimated_tokens=1).name == 'fast-free'
    assert policy.select('vision', required_capabilities=('vision',)).name == 'vision-low'
    assert policy.select('memory', estimated_tokens=1).name == 'fast-free'


def test_model_policy_is_deterministic_and_falls_back_after_failure() -> None:
    policy = ModelPolicy((
        ModelProfile('z-expensive', roles=('main',), capabilities=('text',), cost=9),
        ModelProfile('a-cheap', roles=('main',), capabilities=('text',), cost=1),
        ModelProfile('b-free', roles=('main',), capabilities=('text',), cost=99, free_tokens=1),
    ))
    assert policy.select('main', estimated_tokens=1).name == 'b-free'
    assert policy.select('main', failed_models=('b-free',), estimated_tokens=1).name == 'a-cheap'
    assert policy.select('main', failed_models=('b-free', 'a-cheap')).name == 'z-expensive'

    try:
        policy.select('vision')
    except LookupError:
        pass
    else:
        raise AssertionError('unsupported role was selected')


def test_expired_or_insufficient_free_quota_is_not_preferred() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    policy = ModelPolicy((
        ModelProfile('expired-free', roles=('main',), capabilities=('text',), cost=9, free_tokens=999, free_tokens_expires_at=now - timedelta(seconds=1)),
        ModelProfile('paid', roles=('main',), capabilities=('text',), cost=1),
    ))
    selection = policy.select('main', estimated_tokens=10, now=now)
    assert selection.name == 'paid'
    assert selection.reason == 'lowest eligible cost after free quota'
