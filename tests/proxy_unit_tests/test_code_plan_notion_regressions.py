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


def _metadata(**overrides):
    base = {
        "code_plan_quota_5h": 100,
        "code_plan_quota_weekly": 500,
        "code_plan_week_anchor_epoch": int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()),
    }
    base.update(overrides)
    return base


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
    assert sum(
        amount
        for (sub_id, _name, _period_id), amount in store.spent.items()
        if sub_id == "sub-1"
    ) == 0
