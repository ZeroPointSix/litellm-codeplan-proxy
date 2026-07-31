from datetime import datetime, timezone

import pytest

from litellm.product.quotas.models import (
    CreditMultipliers,
    CreditUsage,
    QuotaEventType,
    QuotaReserveRequest,
    QuotaSettlementRequest,
)
from litellm.product.quotas.service import (
    InMemoryQuotaStore,
    QuotaService,
    quota_windows_from_metadata,
    settlement_usage_for_failure,
)
from litellm.product.subscriptions.litellm_client import (
    ProxySubscriptionLiteLLMClient,
    merge_code_plan_key_metadata,
)
from litellm.proxy.hooks.code_plan_quota import (
    CODE_PLAN_QUOTA_RESERVATION_METADATA_KEY,
    _PROXY_CodePlanQuotaHandler,
)


def _metadata(**overrides):
    base = {
        "code_plan_quota_5h": 100,
        "code_plan_quota_weekly": 500,
        "code_plan_week_anchor_epoch": int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()),
    }
    base.update(overrides)
    return base


def _hook_metadata(**overrides):
    base = {
        **_metadata(),
        "code_plan_subscription_id": "sub-1",
        "code_plan_project_id": "project-1",
        "code_plan_default_max_output_tokens": 100,
        "code_plan_credit_input_multiplier": 1,
        "code_plan_credit_output_multiplier": 1,
        "code_plan_credit_cache_read_multiplier": 0,
        "code_plan_credit_cache_write_multiplier": 0,
    }
    base.update(overrides)
    return base


def _spent(store, subscription_id: str, window_name: str) -> float:
    return sum(
        amount
        for (sub_id, name, _period_id), amount in store.spent.items()
        if sub_id == subscription_id and name == window_name
    )


class _UserKey:
    def __init__(self, metadata):
        self.metadata = metadata
        self.parent_otel_span = None


def test_caller_metadata_cannot_inject_code_plan_anchor():
    merged = merge_code_plan_key_metadata(
        {"code_plan_quota_5h": 100, "code_plan_week_anchor_epoch": 1},
        {
            "code_plan_5h_anchor_epoch": 999,
            "code_plan_5h_period_id": "5h:999",
            "caller_trace": "keep",
        },
    )

    assert merged["caller_trace"] == "keep"
    assert merged["code_plan_quota_5h"] == 100
    assert "code_plan_5h_anchor_epoch" not in merged
    assert "code_plan_5h_period_id" not in merged


class _Subscription:
    def __init__(self, metadata):
        self.metadata = metadata
        self.window_5h_start = None


def test_only_subscription_metadata_can_preserve_existing_5h_anchor():
    client = ProxySubscriptionLiteLLMClient()

    assert client._preserved_5h_anchor_metadata(_Subscription({})) == {}
    assert client._preserved_5h_anchor_metadata(
        _Subscription(
            {
                "code_plan_5h_anchor_epoch": 123,
                "code_plan_5h_period_id": "5h:123",
            }
        )
    ) == {"code_plan_5h_anchor_epoch": 123, "code_plan_5h_period_id": "5h:123"}


@pytest.mark.asyncio
async def test_quota_hook_ignores_key_metadata_5h_anchor_injection(monkeypatch):
    store = InMemoryQuotaStore()
    handler = _PROXY_CodePlanQuotaHandler(
        QuotaService(store),
        pending_compensation_interval_seconds=0,
    )
    now_epoch = int(datetime.now(timezone.utc).timestamp())
    user_api_key = _UserKey(
        _hook_metadata(
            code_plan_week_anchor_epoch=now_epoch - 3600,
            code_plan_5h_anchor_epoch=now_epoch - 60,
            code_plan_5h_period_id=f"5h:{now_epoch - 60}",
        )
    )
    data = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "hello"}],
        "metadata": {"request_id": "req-anchor-injection"},
    }
    monkeypatch.setattr("litellm.proxy.hooks.code_plan_quota.litellm.token_counter", lambda **kwargs: 10)

    await handler.async_pre_call_hook(
        user_api_key_dict=user_api_key,
        cache=None,
        data=data,
        call_type="acompletion",
    )

    reservation = data["metadata"][CODE_PLAN_QUOTA_RESERVATION_METADATA_KEY]
    five_h = next(window for window in reservation["windows"] if window["name"] == "5h")
    assert five_h["starts_on_first_success"] is True
    assert str(five_h["period_id"]).startswith("5h-open:")
    assert store.anchors_5h == {}


@pytest.mark.asyncio
async def test_quota_hook_pre_call_sweeps_expired_pending_usage(monkeypatch):
    store = InMemoryQuotaStore()
    request_ids = iter(("req-pending-runtime", "req-next-runtime"))
    handler = _PROXY_CodePlanQuotaHandler(
        QuotaService(store),
        pending_compensation_interval_seconds=1,
        request_id_factory=request_ids.__next__,
    )
    user_api_key = _UserKey(_hook_metadata())
    monkeypatch.setattr("litellm.proxy.hooks.code_plan_quota.litellm.token_counter", lambda **kwargs: 10)
    pending_data = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "pending"}],
        "metadata": {"request_id": "req-pending-runtime"},
    }

    await handler.async_pre_call_hook(
        user_api_key_dict=user_api_key,
        cache=None,
        data=pending_data,
        call_type="acompletion",
    )
    reserved = _spent(store, "sub-1", "5h")
    await handler.async_release_max_parallel_requests_on_disconnect(user_api_key, pending_data)
    store.reservations[("sub-1", "req-pending-runtime")]["pending_since_epoch"] = 1
    handler._last_pending_compensation_sweep_monotonic = -10_000

    next_data = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "next"}],
        "metadata": {"request_id": "req-next-runtime"},
    }
    await handler.async_pre_call_hook(
        user_api_key_dict=user_api_key,
        cache=None,
        data=next_data,
        call_type="acompletion",
    )

    assert ("sub-1", "req-pending-runtime") not in store.reservations
    assert (
        store.events[("sub-1", "req-pending-runtime", QuotaEventType.EXPIRED)].metadata["reservation_state"]
        == "EXPIRED"
    )
    assert store.events[("sub-1", "req-pending-runtime", QuotaEventType.COMPENSATED)].credits == 0
    assert _spent(store, "sub-1", "5h") == reserved
    assert ("sub-1", "req-next-runtime") in store.reservations


@pytest.mark.asyncio
async def test_upstream_5xx_charges_input_without_opening_first_success_anchor():
    store = InMemoryQuotaStore()
    service = QuotaService(store)
    now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    windows = quota_windows_from_metadata(_metadata(), now=now)
    assert windows[0].starts_on_first_success is True

    reserve = await service.reserve(
        QuotaReserveRequest(
            request_id="req-5xx-before-success",
            subscription_id="sub-1",
            input_usage=CreditUsage(input_tokens=10),
            multipliers=CreditMultipliers(input_multiplier=1, output_multiplier=1),
            windows=windows,
            default_max_output_tokens=100,
        )
    )
    settled = await service.settle(
        QuotaSettlementRequest(
            request_id="req-5xx-before-success",
            subscription_id="sub-1",
            actual_usage=settlement_usage_for_failure(CreditUsage(input_tokens=10), status_code=503),
            multipliers=CreditMultipliers(input_multiplier=1, output_multiplier=1),
            windows=windows,
            reserved_credits=reserve.credits,
            event_type=QuotaEventType.SETTLE,
            anchor_first_success=False,
        )
    )

    assert settled.credits == 10
    assert store.anchors_5h == {}
    settled_windows = settled.metadata["windows"]
    five_h = next(window for window in settled_windows if window["name"] == "5h")
    assert five_h["starts_on_first_success"] is True
    assert str(five_h["period_id"]).startswith("5h-open:")


@pytest.mark.asyncio
async def test_expired_pending_usage_is_compensated_and_releases_hold():
    store = InMemoryQuotaStore()
    service = QuotaService(store)
    windows = quota_windows_from_metadata(
        _metadata(),
        now=datetime(2026, 1, 1, 12, tzinfo=timezone.utc),
    )
    reserve = await service.reserve(
        QuotaReserveRequest(
            request_id="req-pending-expire",
            subscription_id="sub-1",
            input_usage=CreditUsage(input_tokens=10),
            multipliers=CreditMultipliers(input_multiplier=1, output_multiplier=1),
            windows=windows,
            default_max_output_tokens=100,
        )
    )
    pending = await service.settle(
        QuotaSettlementRequest(
            request_id="req-pending-expire",
            subscription_id="sub-1",
            actual_usage=CreditUsage(),
            multipliers=CreditMultipliers(input_multiplier=1, output_multiplier=1),
            windows=windows,
            reserved_credits=reserve.credits,
            event_type=QuotaEventType.PENDING_USAGE,
            anchor_first_success=False,
        )
    )
    assert pending.metadata["reservation_state"] == "PENDING_USAGE"
    assert ("sub-1", "req-pending-expire") in store.reservations

    decisions = await service.compensate_expired_pending_usage(older_than_epoch=10**12)

    assert [decision.event_type for decision in decisions] == [QuotaEventType.EXPIRED, QuotaEventType.COMPENSATED]
    assert decisions[-1].metadata["reservation_state"] == "COMPENSATED"
    assert ("sub-1", "req-pending-expire") not in store.reservations
    assert sum(amount for (sub_id, _name, _period_id), amount in store.spent.items() if sub_id == "sub-1") == 0
