from datetime import datetime, timezone

import pytest

from litellm.product.quotas.models import (
    CreditMultipliers,
    CreditUsage,
    QuotaDecision,
    QuotaEventType,
    QuotaSettlementRequest,
    QuotaWindow,
)
from litellm.product.usage_ledger.models import (
    UsageLedgerEventType,
    UsageLedgerManualAdjustRequest,
    UsageLedgerRecord,
)
from litellm.product.usage_ledger.service import UsageLedgerService


class _UsageLedgerRepository:
    def __init__(self):
        self.created = []
        self.window_updates = []

    async def create(self, data):
        self.created.append(data)
        return UsageLedgerRecord(
            event_id=f"evt-{len(self.created)}",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            **data.model_dump(),
        )

    async def update_subscription_windows(self, **kwargs):
        self.window_updates.append(kwargs)


def _window(name: str, anchor: int, seconds: int) -> QuotaWindow:
    return QuotaWindow(
        name=name,
        limit=1000,
        ttl_seconds=seconds,
        period_id=f"{name}:{anchor}",
        period_end_epoch=anchor + seconds,
        anchor_epoch=anchor,
        starts_on_first_success=False,
    )


@pytest.mark.asyncio
async def test_usage_ledger_service_records_quota_event_windows_and_rule_version():
    repository = _UsageLedgerRepository()
    service = UsageLedgerService(repository)
    anchor = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())
    windows = [_window("5h", anchor, 18_000), _window("week", anchor, 604_800)]
    decision = QuotaDecision(
        allowed=True,
        request_id="req-ledger-service",
        subscription_id="sub-ledger-service",
        project_id="project-ledger-service",
        event_type=QuotaEventType.COMPENSATED,
        credits=0,
        balances_before={"5h": 10},
        balances_after={"5h": 10},
        metadata={"windows": [window.model_dump() for window in windows]},
    )
    request = QuotaSettlementRequest(
        request_id="req-ledger-service",
        subscription_id="sub-ledger-service",
        project_id="project-ledger-service",
        user_id="user-ledger-service",
        api_key_id="key-ledger-service",
        model="ledger-model",
        actual_usage=CreditUsage(),
        multipliers=CreditMultipliers(input_multiplier=2, output_multiplier=3),
        windows=windows,
        reserved_credits=0,
        event_type=QuotaEventType.COMPENSATED,
        rule_version=13,
    )

    await service.record_quota_event(decision=decision, request=request, usage=CreditUsage())

    event = repository.created[0]
    assert event.event_type == UsageLedgerEventType.COMPENSATE
    assert event.rule_version == 13
    assert event.model == "ledger-model"
    assert event.window_5h_period_id == f"5h:{anchor}"
    assert event.window_5h_start == datetime.fromtimestamp(anchor, tz=timezone.utc)
    assert event.window_week_start == datetime.fromtimestamp(anchor, tz=timezone.utc)
    assert repository.window_updates == [
        {
            "subscription_id": "sub-ledger-service",
            "window_5h_start": event.window_5h_start,
            "window_week_start": event.window_week_start,
        }
    ]


@pytest.mark.asyncio
async def test_usage_ledger_manual_adjust_creates_manual_adjust_event():
    repository = _UsageLedgerRepository()
    service = UsageLedgerService(repository)

    record = await service.manual_adjust(
        UsageLedgerManualAdjustRequest(
            subscription_id="sub-manual",
            request_id="manual-1",
            credits=25,
            project_id="project-manual",
            user_id="user-manual",
            model="manual-model",
            rule_version=3,
            metadata={"reason": "support credit"},
        )
    )

    assert record.event_type == UsageLedgerEventType.MANUAL_ADJUST
    assert record.credits == 25
    assert record.rule_version == 3
    assert record.metadata == {"reason": "support credit"}
