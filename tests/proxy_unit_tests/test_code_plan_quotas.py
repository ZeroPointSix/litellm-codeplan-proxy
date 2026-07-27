import asyncio
import os
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from litellm.product.quotas.models import (
    CreditMultipliers,
    CreditUsage,
    QuotaEventType,
    QuotaReserveRequest,
    QuotaSettlementRequest,
    QuotaWindow,
)
from litellm.product.quotas.redis_store import RedisQuotaStore
from litellm.product.quotas.service import (
    InMemoryQuotaStore,
    QuotaExceededError,
    QuotaService,
    QuotaUnavailableError,
    calculate_credits,
    quota_windows_from_metadata,
    settlement_usage_for_failure,
)


def _spent(store, subscription_id: str, window_name: str) -> float:
    return sum(
        amount
        for (sub_id, name, _period_id), amount in store.spent.items()
        if sub_id == subscription_id and name == window_name
    )


from litellm.proxy.hooks.code_plan_quota import (
    CODE_PLAN_QUOTA_RESERVATION_METADATA_KEY,
    _PROXY_CodePlanQuotaHandler,
)


def _windows(
    limit_5h: float = 100, limit_week: float = 500, limit_month: float = 1000, *, now_epoch: int | None = None
):
    now = now_epoch if now_epoch is not None else int(datetime.now(timezone.utc).timestamp())
    return [
        QuotaWindow(
            name="5h",
            limit=limit_5h,
            ttl_seconds=18_000,
            period_id=f"5h:{now}",
            period_end_epoch=now + 18_000,
            anchor_epoch=now,
            starts_on_first_success=False,
        ),
        QuotaWindow(
            name="week",
            limit=limit_week,
            ttl_seconds=604_800,
            period_id=f"week:{now}",
            period_end_epoch=now + 604_800,
            anchor_epoch=now,
            starts_on_first_success=False,
        ),
        QuotaWindow(
            name="month",
            limit=limit_month,
            ttl_seconds=2_592_000,
            period_id=f"month:{now}",
            period_end_epoch=now + 2_592_000,
            anchor_epoch=now,
            starts_on_first_success=False,
        ),
    ]


class _UserKey:
    def __init__(self, metadata):
        self.metadata = metadata
        self.parent_otel_span = None


def _code_plan_metadata(**overrides):
    metadata = {
        "code_plan_subscription_id": "sub-1",
        "code_plan_project_id": "project-1",
        "code_plan_quota_5h": 100,
        "code_plan_quota_weekly": 500,
        "code_plan_default_max_output_tokens": 100,
        "code_plan_credit_input_multiplier": 1,
        "code_plan_credit_output_multiplier": 1,
        "code_plan_credit_cache_read_multiplier": 0,
        "code_plan_credit_cache_write_multiplier": 0,
    }
    metadata.update(overrides)
    return metadata


def test_calculate_credits_uses_all_four_multipliers():
    usage = CreditUsage(
        input_tokens=10,
        output_tokens=5,
        cache_read_tokens=20,
        cache_write_tokens=2,
    )
    multipliers = CreditMultipliers(
        input_multiplier=2,
        output_multiplier=3,
        cache_read_multiplier=0.5,
        cache_write_multiplier=4,
    )

    assert calculate_credits(usage, multipliers) == 53


def test_quota_windows_from_metadata_supports_monthly_window():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    windows = quota_windows_from_metadata(
        {
            "code_plan_quota_5h": 100,
            "code_plan_quota_weekly": 500,
            "code_plan_quota_monthly": 2000,
            "code_plan_week_anchor_epoch": int(now.timestamp()),
        },
        now=now,
    )

    assert [window.name for window in windows] == ["5h", "week", "month"]
    assert [window.limit for window in windows] == [100, 500, 2000]
    assert windows[0].starts_on_first_success is True
    assert windows[1].period_id.startswith("week:")
    assert windows[1].ttl_seconds == 7 * 24 * 60 * 60


def test_quota_windows_are_fixed_anchor_not_sliding():
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    week_anchor = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())
    metadata = {
        "code_plan_quota_5h": 100,
        "code_plan_quota_weekly": 500,
        "code_plan_week_anchor_epoch": week_anchor,
        "code_plan_5h_anchor_epoch": int(now.timestamp()) - 3600,
    }
    first = quota_windows_from_metadata(metadata, now=now)
    later = quota_windows_from_metadata(metadata, now=now.replace(hour=13))
    # Fixed 5h window end does not slide forward with later calls.
    assert first[0].period_end_epoch == later[0].period_end_epoch
    assert first[0].ttl_seconds == later[0].ttl_seconds + 3600
    assert first[1].period_id == later[1].period_id


def test_weekly_reset_clears_unopened_5h_period_id():
    week_anchor = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())
    early = datetime(2026, 1, 2, tzinfo=timezone.utc)
    after_week = datetime(2026, 1, 9, tzinfo=timezone.utc)
    metadata = {
        "code_plan_quota_5h": 100,
        "code_plan_quota_weekly": 500,
        "code_plan_week_anchor_epoch": week_anchor,
    }
    w1 = quota_windows_from_metadata(metadata, now=early)
    w2 = quota_windows_from_metadata(metadata, now=after_week)
    assert w1[0].period_id != w2[0].period_id
    assert w1[1].period_id != w2[1].period_id


@pytest.mark.asyncio
async def test_reserve_then_settle_true_up_releases_unused_credits():
    service = QuotaService(InMemoryQuotaStore())
    multipliers = CreditMultipliers(input_multiplier=1, output_multiplier=2)
    reserve = await service.reserve(
        QuotaReserveRequest(
            request_id="req-1",
            subscription_id="sub-1",
            project_id="project-1",
            input_usage=CreditUsage(input_tokens=10),
            multipliers=multipliers,
            windows=_windows(),
            default_max_output_tokens=20,
        )
    )

    assert reserve.credits == 50
    assert reserve.balances_before["5h"] == 100
    assert reserve.balances_after["5h"] == 50

    settled = await service.settle(
        QuotaSettlementRequest(
            request_id="req-1",
            subscription_id="sub-1",
            project_id="project-1",
            actual_usage=CreditUsage(input_tokens=10, output_tokens=5),
            multipliers=multipliers,
            windows=_windows(),
            reserved_credits=reserve.credits,
        )
    )

    assert settled.credits == 20
    assert settled.balances_after["5h"] == 80
    assert settled.balances_after["week"] == 480
    assert settled.balances_after["month"] == 980


@pytest.mark.asyncio
async def test_reserve_is_idempotent_by_request_and_event_type():
    store = InMemoryQuotaStore()
    service = QuotaService(store)
    request = QuotaReserveRequest(
        request_id="req-idempotent",
        subscription_id="sub-1",
        input_usage=CreditUsage(input_tokens=10),
        multipliers=CreditMultipliers(),
        windows=_windows(),
        default_max_output_tokens=5,
    )

    first = await service.reserve(request)
    second = await service.reserve(request)

    assert first.credits == 15
    assert second.idempotent is True
    assert _spent(store, "sub-1", "5h") == 15


@pytest.mark.asyncio
async def test_settle_is_idempotent_by_request_and_event_type():
    store = InMemoryQuotaStore()
    service = QuotaService(store)
    reserve = await service.reserve(
        QuotaReserveRequest(
            request_id="req-settle",
            subscription_id="sub-1",
            input_usage=CreditUsage(input_tokens=10),
            multipliers=CreditMultipliers(),
            windows=_windows(),
            default_max_output_tokens=5,
        )
    )
    request = QuotaSettlementRequest(
        request_id="req-settle",
        subscription_id="sub-1",
        actual_usage=CreditUsage(input_tokens=10, output_tokens=2),
        multipliers=CreditMultipliers(),
        windows=_windows(),
        reserved_credits=reserve.credits,
    )

    first = await service.settle(request)
    second = await service.settle(request)

    assert first.credits == 12
    assert second.idempotent is True
    assert _spent(store, "sub-1", "5h") == 12


@pytest.mark.asyncio
async def test_rejected_reserve_is_not_cached_and_can_succeed_after_recovery():
    store = InMemoryQuotaStore()
    service = QuotaService(store)
    windows = _windows(limit_5h=1, limit_week=1, limit_month=1)
    first = await service.reserve(
        QuotaReserveRequest(
            request_id="req-prime-overdraft",
            subscription_id="sub-1",
            input_usage=CreditUsage(input_tokens=2),
            multipliers=CreditMultipliers(input_multiplier=1, output_multiplier=0),
            windows=windows,
            default_max_output_tokens=0,
        )
    )
    retry_request = QuotaReserveRequest(
        request_id="req-retry-after-reject",
        subscription_id="sub-1",
        input_usage=CreditUsage(input_tokens=1),
        multipliers=CreditMultipliers(input_multiplier=1, output_multiplier=0),
        windows=windows,
        default_max_output_tokens=0,
    )

    with pytest.raises(QuotaExceededError):
        await service.reserve(retry_request)

    await service.settle(
        QuotaSettlementRequest(
            request_id="req-prime-overdraft",
            subscription_id="sub-1",
            actual_usage=CreditUsage(),
            multipliers=CreditMultipliers(input_multiplier=1, output_multiplier=0),
            windows=windows,
            reserved_credits=first.credits,
            event_type=QuotaEventType.RELEASE,
        )
    )
    retried = await service.reserve(retry_request)

    assert retried.allowed is True
    assert retried.idempotent is False
    assert _spent(store, "sub-1", "5h") == 1


@pytest.mark.asyncio
async def test_redis_rejected_reserve_is_not_cached_and_can_succeed_after_recovery():
    if os.getenv("CODE_PLAN_REDIS_INTEGRATION") != "true":
        pytest.skip("Redis integration test disabled")
    redis_url = os.getenv("CODE_PLAN_QUOTA_REDIS_URL")
    if not redis_url:
        pytest.skip("CODE_PLAN_QUOTA_REDIS_URL is not configured")

    redis_asyncio = pytest.importorskip("redis.asyncio")
    redis_client = redis_asyncio.from_url(redis_url, decode_responses=True)
    key_prefix = f"codeplan:test:{uuid4()}"
    service = QuotaService(RedisQuotaStore(redis_client, key_prefix=key_prefix, event_ttl_seconds=60))
    windows = _windows(limit_5h=1, limit_week=1, limit_month=1)
    try:
        first = await service.reserve(
            QuotaReserveRequest(
                request_id="req-prime-overdraft",
                subscription_id="sub-redis",
                input_usage=CreditUsage(input_tokens=2),
                multipliers=CreditMultipliers(input_multiplier=1, output_multiplier=0),
                windows=windows,
                default_max_output_tokens=0,
            )
        )
        retry_request = QuotaReserveRequest(
            request_id="req-retry-after-reject",
            subscription_id="sub-redis",
            input_usage=CreditUsage(input_tokens=1),
            multipliers=CreditMultipliers(input_multiplier=1, output_multiplier=0),
            windows=windows,
            default_max_output_tokens=0,
        )

        with pytest.raises(QuotaExceededError):
            await service.reserve(retry_request)

        await service.settle(
            QuotaSettlementRequest(
                request_id="req-prime-overdraft",
                subscription_id="sub-redis",
                actual_usage=CreditUsage(),
                multipliers=CreditMultipliers(input_multiplier=1, output_multiplier=0),
                windows=windows,
                reserved_credits=first.credits,
                event_type=QuotaEventType.RELEASE,
            )
        )
        retried = await service.reserve(retry_request)

        assert retried.allowed is True
        assert retried.idempotent is False
    finally:
        keys = await redis_client.keys(f"{key_prefix}:*")
        if keys:
            await redis_client.delete(*keys)
        close = getattr(redis_client, "aclose", redis_client.close)
        await close()


@pytest.mark.asyncio
async def test_redis_settle_uses_stored_reserved_credits_not_request_payload():
    if os.getenv("CODE_PLAN_REDIS_INTEGRATION") != "true":
        pytest.skip("Redis integration test disabled")
    redis_url = os.getenv("CODE_PLAN_QUOTA_REDIS_URL")
    if not redis_url:
        pytest.skip("CODE_PLAN_QUOTA_REDIS_URL is not configured")

    redis_asyncio = pytest.importorskip("redis.asyncio")
    redis_client = redis_asyncio.from_url(redis_url, decode_responses=True)
    key_prefix = f"codeplan:test:{uuid4()}"
    store = RedisQuotaStore(redis_client, key_prefix=key_prefix, event_ttl_seconds=60)
    service = QuotaService(store)
    windows = _windows(limit_5h=500, limit_week=500, limit_month=500)
    multipliers = CreditMultipliers(input_multiplier=1, output_multiplier=1)
    try:
        reserve = await service.reserve(
            QuotaReserveRequest(
                request_id="req-redis-trust",
                subscription_id="sub-redis-trust",
                input_usage=CreditUsage(input_tokens=10),
                multipliers=multipliers,
                windows=windows,
                default_max_output_tokens=20,
            )
        )
        assert reserve.credits == 30

        settled = await service.settle(
            QuotaSettlementRequest(
                request_id="req-redis-trust",
                subscription_id="sub-redis-trust",
                actual_usage=CreditUsage(input_tokens=10, output_tokens=5),
                multipliers=multipliers,
                windows=windows,
                reserved_credits=0,
            )
        )

        assert settled.allowed is True
        assert settled.credits == 15
        assert settled.balances_after["5h"] == 485
    finally:
        keys = await redis_client.keys(f"{key_prefix}:*")
        if keys:
            await redis_client.delete(*keys)
        close = getattr(redis_client, "aclose", redis_client.close)
        await close()


@pytest.mark.asyncio
async def test_quota_hook_disconnect_only_releases_matching_request(monkeypatch):
    store = InMemoryQuotaStore()
    handler = _PROXY_CodePlanQuotaHandler(QuotaService(store))
    user_api_key = _UserKey(metadata=_code_plan_metadata(code_plan_quota_5h=1000, code_plan_quota_weekly=10000))
    data_a = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "a"}],
        "metadata": {"request_id": "req-a"},
    }
    data_b = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "b"}],
        "metadata": {"request_id": "req-b"},
    }
    monkeypatch.setattr("litellm.proxy.hooks.code_plan_quota.litellm.token_counter", lambda **kwargs: 10)

    await handler.async_pre_call_hook(
        user_api_key_dict=user_api_key,
        cache=None,
        data=data_a,
        call_type="acompletion",
    )
    spent_after_a = _spent(store, "sub-1", "5h")
    await handler.async_pre_call_hook(
        user_api_key_dict=user_api_key,
        cache=None,
        data=data_b,
        call_type="acompletion",
    )
    assert _spent(store, "sub-1", "5h") == spent_after_a + 110

    # No produced usage on disconnect => PENDING_USAGE holds req-a reservation.
    await handler.async_release_max_parallel_requests_on_disconnect(user_api_key, data_a)

    assert _spent(store, "sub-1", "5h") == spent_after_a + 110
    assert ("sub-1", "req-a") in store.reservations
    assert store.reservations[("sub-1", "req-a")]["state"] == "PENDING_USAGE"
    assert ("sub-1", "req-b") in store.reservations

    # Later compensation with empty usage releases only req-a.
    await handler._settle_reserved_usage(
        data=data_a,
        metadata=user_api_key.metadata,
        reservation=data_a["metadata"][CODE_PLAN_QUOTA_RESERVATION_METADATA_KEY],
        actual_usage=CreditUsage(),
        event_type=QuotaEventType.RELEASE,
        context="compensate empty",
    )
    assert _spent(store, "sub-1", "5h") == 110
    assert ("sub-1", "req-a") not in store.reservations
    assert ("sub-1", "req-b") in store.reservations

    await handler.async_log_success_event(
        kwargs={
            "litellm_params": {"metadata": data_b["metadata"]},
            "standard_logging_object": {"metadata": data_b["metadata"]},
        },
        response_obj={"usage": {"prompt_tokens": 10, "completion_tokens": 5}},
        start_time=None,
        end_time=None,
    )
    assert _spent(store, "sub-1", "5h") == 15


def test_redis_reservation_ttl_covers_longest_window():
    store = RedisQuotaStore(object(), key_prefix="codeplan:test", event_ttl_seconds=86400)
    windows = _windows()
    ttl = store._ttl_for_windows(windows)
    assert ttl >= max(window.ttl_seconds for window in windows)
    assert ttl >= 86400


@pytest.mark.asyncio
async def test_quota_allows_one_overdraft_only_when_balance_started_positive():
    service = QuotaService(InMemoryQuotaStore())
    windows = _windows(limit_5h=10, limit_week=10, limit_month=10)

    overdraft = await service.reserve(
        QuotaReserveRequest(
            request_id="req-overdraft-1",
            subscription_id="sub-1",
            input_usage=CreditUsage(input_tokens=11),
            multipliers=CreditMultipliers(input_multiplier=1, output_multiplier=0),
            windows=windows,
            default_max_output_tokens=0,
        )
    )

    assert overdraft.balances_before["5h"] == 10
    assert overdraft.balances_after["5h"] == -1

    with pytest.raises(QuotaExceededError) as exc:
        await service.reserve(
            QuotaReserveRequest(
                request_id="req-overdraft-2",
                subscription_id="sub-1",
                input_usage=CreditUsage(input_tokens=1),
                multipliers=CreditMultipliers(input_multiplier=1, output_multiplier=0),
                windows=windows,
                default_max_output_tokens=0,
            )
        )

    assert exc.value.decision.balances_before["5h"] == -1


@pytest.mark.asyncio
async def test_failed_non_5xx_request_releases_reservation():
    service = QuotaService(InMemoryQuotaStore())
    reserve_request = QuotaReserveRequest(
        request_id="req-fail-400",
        subscription_id="sub-1",
        input_usage=CreditUsage(input_tokens=10),
        multipliers=CreditMultipliers(),
        windows=_windows(),
        default_max_output_tokens=100,
    )
    reserve = await service.reserve(reserve_request)

    settled = await service.settle(
        QuotaSettlementRequest(
            request_id="req-fail-400",
            subscription_id="sub-1",
            actual_usage=settlement_usage_for_failure(reserve_request.input_usage, status_code=400),
            multipliers=reserve_request.multipliers,
            windows=reserve_request.windows,
            reserved_credits=reserve.credits,
        )
    )

    assert reserve.credits == 110
    assert settled.credits == 0
    assert settled.balances_after["5h"] == 100


@pytest.mark.asyncio
async def test_upstream_5xx_charges_input_only():
    service = QuotaService(InMemoryQuotaStore())
    reserve_request = QuotaReserveRequest(
        request_id="req-fail-500",
        subscription_id="sub-1",
        input_usage=CreditUsage(input_tokens=10),
        multipliers=CreditMultipliers(),
        windows=_windows(),
        default_max_output_tokens=100,
    )
    reserve = await service.reserve(reserve_request)

    settled = await service.settle(
        QuotaSettlementRequest(
            request_id="req-fail-500",
            subscription_id="sub-1",
            actual_usage=settlement_usage_for_failure(reserve_request.input_usage, status_code=503),
            multipliers=reserve_request.multipliers,
            windows=reserve_request.windows,
            reserved_credits=reserve.credits,
        )
    )

    assert reserve.credits == 110
    assert settled.credits == 10
    assert settled.balances_after["5h"] == 90


@pytest.mark.asyncio
async def test_quota_hook_streaming_success_settles_and_post_call_does_not_double_settle(monkeypatch):
    store = InMemoryQuotaStore()
    handler = _PROXY_CodePlanQuotaHandler(QuotaService(store))
    user_api_key = _UserKey(metadata=_code_plan_metadata())
    data = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "hello"}],
        "metadata": {"request_id": "req-stream-success"},
    }
    monkeypatch.setattr("litellm.proxy.hooks.code_plan_quota.litellm.token_counter", lambda **kwargs: 10)

    await handler.async_pre_call_hook(
        user_api_key_dict=user_api_key,
        cache=None,
        data=data,
        call_type="acompletion",
    )
    assert _spent(store, "sub-1", "5h") == 110

    await handler.async_log_success_event(
        kwargs={
            "litellm_params": {"metadata": data["metadata"]},
            "standard_logging_object": {"metadata": data["metadata"]},
        },
        response_obj={"usage": {"prompt_tokens": 10, "completion_tokens": 5}},
        start_time=None,
        end_time=None,
    )
    await handler.async_post_call_success_hook(
        data=data,
        user_api_key_dict=user_api_key,
        response={"usage": {"prompt_tokens": 10, "completion_tokens": 50}},
    )

    assert _spent(store, "sub-1", "5h") == 15
    assert store.events[("sub-1", "req-stream-success", QuotaEventType.SETTLE)].credits == 15


@pytest.mark.asyncio
async def test_quota_hook_disconnect_settles_produced_tokens(monkeypatch):
    store = InMemoryQuotaStore()
    handler = _PROXY_CodePlanQuotaHandler(QuotaService(store))
    user_api_key = _UserKey(metadata=_code_plan_metadata())
    data = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "hello"}],
        "metadata": {"request_id": "req-stream-disconnect"},
        "combined_usage_object": {"prompt_tokens": 10, "completion_tokens": 3},
    }
    monkeypatch.setattr("litellm.proxy.hooks.code_plan_quota.litellm.token_counter", lambda **kwargs: 10)

    await handler.async_pre_call_hook(
        user_api_key_dict=user_api_key,
        cache=None,
        data=data,
        call_type="acompletion",
    )
    assert CODE_PLAN_QUOTA_RESERVATION_METADATA_KEY in data["metadata"]
    assert _spent(store, "sub-1", "5h") == 110

    await handler.async_release_max_parallel_requests_on_disconnect(user_api_key, data)

    # Confirmed produced tokens are settled (10 input + 3 output).
    assert _spent(store, "sub-1", "5h") == 13
    assert store.events[("sub-1", "req-stream-disconnect", QuotaEventType.SETTLE)].credits == 13


@pytest.mark.asyncio
async def test_quota_hook_disconnect_without_usage_marks_pending(monkeypatch):
    store = InMemoryQuotaStore()
    handler = _PROXY_CodePlanQuotaHandler(QuotaService(store))
    user_api_key = _UserKey(metadata=_code_plan_metadata())
    data = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "hello"}],
        "metadata": {"request_id": "req-stream-pending"},
    }
    monkeypatch.setattr("litellm.proxy.hooks.code_plan_quota.litellm.token_counter", lambda **kwargs: 10)

    await handler.async_pre_call_hook(
        user_api_key_dict=user_api_key,
        cache=None,
        data=data,
        call_type="acompletion",
    )
    reserved = _spent(store, "sub-1", "5h")
    await handler.async_release_max_parallel_requests_on_disconnect(user_api_key, data)

    # No confirmed usage yet: keep reservation held for compensation.
    assert _spent(store, "sub-1", "5h") == reserved
    assert ("sub-1", "req-stream-pending") in store.reservations
    assert store.events[("sub-1", "req-stream-pending", QuotaEventType.PENDING_USAGE)].credits == reserved


def test_quota_hook_token_counter_failure_uses_text_fallback(monkeypatch):
    handler = _PROXY_CodePlanQuotaHandler(QuotaService(InMemoryQuotaStore()))

    def _raise_token_counter(**kwargs):
        raise RuntimeError("unknown model")

    monkeypatch.setattr("litellm.proxy.hooks.code_plan_quota.litellm.token_counter", _raise_token_counter)

    estimate = handler._estimate_input_tokens(
        {
            "model": "unknown-model",
            "messages": [{"role": "user", "content": "x" * 20}],
        }
    )

    assert estimate > 0


@pytest.mark.asyncio
async def test_settle_without_reservation_returns_not_allowed():
    service = QuotaService(InMemoryQuotaStore())
    decision = await service.settle(
        QuotaSettlementRequest(
            request_id="req-missing",
            subscription_id="sub-1",
            actual_usage=CreditUsage(input_tokens=1),
            multipliers=CreditMultipliers(),
            windows=_windows(),
            reserved_credits=10,
        )
    )

    assert decision.allowed is False
    assert decision.reason == "Quota reservation not found"


@pytest.mark.asyncio
async def test_quota_hook_does_not_mark_settled_when_store_rejects_settlement(monkeypatch):
    class RejectingStore(InMemoryQuotaStore):
        async def settle(self, request, settled_credits):
            return await super().settle(request, settled_credits)

    store = RejectingStore()
    store.reservations.clear()
    handler = _PROXY_CodePlanQuotaHandler(QuotaService(store))
    user_api_key = _UserKey(metadata=_code_plan_metadata())
    reservation = {
        "request_id": "req-reject-settle",
        "subscription_id": "sub-1",
        "project_id": "project-1",
        "reserved_credits": 10.0,
        "input_usage": {"input_tokens": 10, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0},
        "multipliers": CreditMultipliers().model_dump(),
        "windows": [window.model_dump() for window in _windows()],
    }
    data = {"metadata": {CODE_PLAN_QUOTA_RESERVATION_METADATA_KEY: reservation}}

    await handler._settle_reserved_usage(
        data=data,
        metadata=user_api_key.metadata,
        reservation=reservation,
        actual_usage=CreditUsage(input_tokens=10, output_tokens=5),
        event_type=QuotaEventType.SETTLE,
        context="test",
    )

    assert reservation.get("_code_plan_quota_settled") is not True


@pytest.mark.asyncio
async def test_proxy_logging_disconnect_schedules_code_plan_quota_release():
    from litellm.proxy.hooks.code_plan_quota import _PROXY_CodePlanQuotaHandler
    from litellm.proxy.utils import ProxyLogging

    quota_hook = _PROXY_CodePlanQuotaHandler(QuotaService(InMemoryQuotaStore()))
    proxy_logging = ProxyLogging(user_api_key_cache=None)
    proxy_logging.proxy_hook_mapping["code_plan_quota"] = quota_hook
    scheduled: list[str] = []

    async def _track_release(user_api_key_dict, request_data=None):
        scheduled.append("quota")

    quota_hook.async_release_max_parallel_requests_on_disconnect = _track_release

    user_api_key = _UserKey(metadata=_code_plan_metadata())
    request_data = {"metadata": {"request_id": "req-stream-disconnect"}}
    proxy_logging._release_max_parallel_requests_on_disconnect(user_api_key, request_data)
    await asyncio.sleep(0)

    assert scheduled == ["quota"]


@pytest.mark.asyncio
async def test_quota_service_fails_closed_when_store_is_unavailable():
    class FailingStore:
        async def reserve(self, request, reserved_credits):
            raise RuntimeError("redis down")

        async def settle(self, request, settled_credits):
            raise RuntimeError("redis down")

    service = QuotaService(FailingStore())

    with pytest.raises(QuotaUnavailableError):
        await service.reserve(
            QuotaReserveRequest(
                request_id="req-down",
                subscription_id="sub-1",
                input_usage=CreditUsage(input_tokens=1),
                multipliers=CreditMultipliers(),
                windows=_windows(),
                default_max_output_tokens=1,
            )
        )


@pytest.mark.asyncio
async def test_first_success_persists_5h_anchor_for_later_reserves():
    store = InMemoryQuotaStore()
    service = QuotaService(store)
    now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    metadata = {
        "code_plan_quota_5h": 1000,
        "code_plan_quota_weekly": 5000,
        "code_plan_week_anchor_epoch": int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()),
    }
    windows = quota_windows_from_metadata(metadata, now=now)
    assert windows[0].starts_on_first_success is True

    reserve = await service.reserve(
        QuotaReserveRequest(
            request_id="req-anchor-1",
            subscription_id="sub-anchor",
            input_usage=CreditUsage(input_tokens=10),
            multipliers=CreditMultipliers(),
            windows=windows,
            default_max_output_tokens=0,
        )
    )
    settled = await service.settle(
        QuotaSettlementRequest(
            request_id="req-anchor-1",
            subscription_id="sub-anchor",
            actual_usage=CreditUsage(input_tokens=10, output_tokens=2),
            multipliers=CreditMultipliers(),
            windows=windows,
            reserved_credits=reserve.credits,
        )
    )
    assert settled.allowed is True
    anchored_windows = settled.metadata.get("windows")
    assert isinstance(anchored_windows, list)
    five = next(window for window in anchored_windows if window["name"] == "5h")
    assert five["starts_on_first_success"] is False
    assert five["period_id"].startswith("5h:")

    # Later reserve without metadata anchor still uses store-persisted fixed window.
    later_windows = quota_windows_from_metadata(metadata, now=now)
    assert later_windows[0].starts_on_first_success is True
    second = await service.reserve(
        QuotaReserveRequest(
            request_id="req-anchor-2",
            subscription_id="sub-anchor",
            input_usage=CreditUsage(input_tokens=1),
            multipliers=CreditMultipliers(),
            windows=later_windows,
            default_max_output_tokens=0,
        )
    )
    assert second.allowed is True
    week = next(window for window in later_windows if window.name == "week")
    assert store.anchors_5h[("sub-anchor", week.period_id)] == five["anchor_epoch"]
