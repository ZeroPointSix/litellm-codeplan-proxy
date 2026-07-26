import pytest

from litellm.product.quotas.models import (
    CreditMultipliers,
    CreditUsage,
    QuotaReserveRequest,
    QuotaSettlementRequest,
    QuotaWindow,
)
from litellm.product.quotas.service import (
    InMemoryQuotaStore,
    QuotaExceededError,
    QuotaService,
    QuotaUnavailableError,
    calculate_credits,
    quota_windows_from_metadata,
    settlement_usage_for_failure,
)


def _windows(limit_5h: float = 100, limit_week: float = 500, limit_month: float = 1000):
    return [
        QuotaWindow(name="5h", limit=limit_5h, ttl_seconds=18_000),
        QuotaWindow(name="week", limit=limit_week, ttl_seconds=604_800),
        QuotaWindow(name="month", limit=limit_month, ttl_seconds=2_592_000),
    ]


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
    windows = quota_windows_from_metadata(
        {
            "code_plan_quota_5h": 100,
            "code_plan_quota_weekly": 500,
            "code_plan_quota_monthly": 2000,
        }
    )

    assert [window.name for window in windows] == ["5h", "week", "month"]
    assert [window.limit for window in windows] == [100, 500, 2000]


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
    assert store.spent[("sub-1", "5h")] == 15


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
    assert store.spent[("sub-1", "5h")] == 12


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
