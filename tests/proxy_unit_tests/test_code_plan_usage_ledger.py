from datetime import datetime, timezone

import pytest

from litellm.product.quotas.models import (
    CreditMultipliers,
    CreditUsage,
    QuotaAdjustmentResult,
    QuotaDecision,
    QuotaEventType,
    QuotaSettlementRequest,
    QuotaWindow,
)
from litellm.product.usage_ledger.models import (
    UsageLedgerEventType,
    UsageLedgerGroupBy,
    UsageLedgerManualAdjustRequest,
    UsageLedgerRecord,
)
from litellm.product.usage_ledger.service import UsageLedgerService


class _UsageLedgerRepository:
    def __init__(self):
        self.created = []
        self.window_updates = []
        self.aggregates = []
        self.lists = []
        self.subscription_metadata = {}

    async def create(self, data):
        self.created.append(data)
        return UsageLedgerRecord(
            event_id=f"evt-{len(self.created)}",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            **data.model_dump(),
        )

    async def update_subscription_windows(self, **kwargs):
        self.window_updates.append(kwargs)

    async def list(self, **kwargs):
        self.lists.append(kwargs)
        return []

    async def aggregate(self, **kwargs):
        self.aggregates.append(kwargs)
        return []

    async def subscription_quota_metadata(self, subscription_id):
        return self.subscription_metadata[subscription_id]


class _ManualAdjustQuotaService:
    def __init__(self, result: QuotaAdjustmentResult):
        self.result = result
        self.calls = []

    async def manual_adjust(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


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
            credits=-25,
            reason="support debit",
            project_id="project-manual",
            user_id="user-manual",
            model="manual-model",
            rule_version=3,
            metadata={"reason": "support credit"},
        )
    )

    assert record.event_type == UsageLedgerEventType.MANUAL_ADJUST
    assert record.credits == -25
    assert record.rule_version == 3
    assert record.metadata == {"reason": "support debit"}


@pytest.mark.asyncio
async def test_usage_ledger_manual_adjust_records_quota_balance_change():
    repository = _UsageLedgerRepository()
    anchor = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())
    windows = [_window("5h", anchor, 18_000), _window("week", anchor, 604_800)]
    quota_service = _ManualAdjustQuotaService(
        QuotaAdjustmentResult(
            subscription_id="sub-manual",
            request_id="manual-2",
            credits=10,
            balances_before={"5h": 80, "week": 400},
            balances_after={"5h": 90, "week": 410},
            windows=windows,
        )
    )
    repository.subscription_metadata["sub-manual"] = {
        "code_plan_quota_5h": 100,
        "code_plan_quota_weekly": 500,
        "code_plan_week_anchor_epoch": anchor,
    }
    service = UsageLedgerService(repository, quota_service=quota_service)

    record = await service.manual_adjust(
        UsageLedgerManualAdjustRequest(
            subscription_id="sub-manual",
            request_id="manual-2",
            credits=10,
            reason="support credit",
        )
    )

    assert quota_service.calls[0]["subscription_id"] == "sub-manual"
    assert quota_service.calls[0]["credits"] == 10
    assert record.window_5h_period_id == f"5h:{anchor}"
    assert record.metadata["reason"] == "support credit"
    assert record.metadata["balances_before"] == {"5h": 80, "week": 400}
    assert record.metadata["balances_after"] == {"5h": 90, "week": 410}


@pytest.mark.asyncio
async def test_usage_ledger_summary_defaults_to_billable_event_types():
    repository = _UsageLedgerRepository()
    service = UsageLedgerService(repository)

    await service.aggregate_events(group_by=UsageLedgerGroupBy.DAY)

    assert repository.aggregates[0]["event_type"] == [
        UsageLedgerEventType.SETTLE.value,
        UsageLedgerEventType.MANUAL_ADJUST.value,
    ]


@pytest.mark.asyncio
async def test_usage_ledger_summary_respects_explicit_event_type():
    repository = _UsageLedgerRepository()
    service = UsageLedgerService(repository)

    await service.aggregate_events(
        group_by=UsageLedgerGroupBy.DAY,
        event_type=UsageLedgerEventType.RESERVE,
    )

    assert repository.aggregates[0]["event_type"] == UsageLedgerEventType.RESERVE.value


@pytest.mark.asyncio
async def test_usage_ledger_list_filters_by_request_id():
    repository = _UsageLedgerRepository()
    service = UsageLedgerService(repository)

    await service.list_events(request_id="req-chain", limit=1000)

    assert repository.lists[0]["request_id"] == "req-chain"
    assert repository.lists[0]["limit"] == 1000
    assert repository.lists[0]["offset"] == 0


@pytest.mark.asyncio
async def test_usage_ledger_list_supports_offset_pagination():
    repository = _UsageLedgerRepository()
    service = UsageLedgerService(repository)

    await service.list_events(subscription_id="sub-page", limit=1000, offset=1000)

    assert repository.lists[0]["subscription_id"] == "sub-page"
    assert repository.lists[0]["limit"] == 1000
    assert repository.lists[0]["offset"] == 1000


@pytest.mark.asyncio
async def test_usage_ledger_manual_adjust_rolls_back_quota_when_ledger_write_fails():
    class _FailingCreateRepository(_UsageLedgerRepository):
        async def create(self, data):
            raise RuntimeError("db write failed")

    repository = _FailingCreateRepository()
    anchor = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())
    windows = [_window("5h", anchor, 18_000), _window("week", anchor, 604_800)]
    quota_service = _ManualAdjustQuotaService(
        QuotaAdjustmentResult(
            subscription_id="sub-manual",
            request_id="manual-rollback",
            credits=10,
            balances_before={"5h": 80, "week": 400},
            balances_after={"5h": 90, "week": 410},
            windows=windows,
        )
    )
    repository.subscription_metadata["sub-manual"] = {
        "code_plan_quota_5h": 100,
        "code_plan_quota_weekly": 500,
        "code_plan_week_anchor_epoch": anchor,
    }
    service = UsageLedgerService(repository, quota_service=quota_service)

    with pytest.raises(RuntimeError, match="db write failed"):
        await service.manual_adjust(
            UsageLedgerManualAdjustRequest(
                subscription_id="sub-manual",
                request_id="manual-rollback",
                credits=10,
                reason="support credit",
            )
        )

    assert len(quota_service.calls) == 2
    assert quota_service.calls[0]["credits"] == 10
    assert quota_service.calls[1]["credits"] == -10
    assert quota_service.calls[1]["request_id"] == "manual-rollback-rollback"
