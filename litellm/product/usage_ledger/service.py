from __future__ import annotations

from datetime import datetime, timezone

from litellm.product.quotas.models import (
    CreditUsage,
    QuotaDecision,
    QuotaEventType,
    QuotaReserveRequest,
    QuotaSettlementRequest,
    QuotaWindow,
)
from litellm.product.usage_ledger.models import (
    UsageLedgerAggregateRecord,
    UsageLedgerCreate,
    UsageLedgerEventType,
    UsageLedgerGroupBy,
    UsageLedgerManualAdjustRequest,
    UsageLedgerRecord,
)
from litellm.product.usage_ledger.repository import UsageLedgerRepository


class UsageLedgerService:
    def __init__(self, repository: UsageLedgerRepository):
        self.repository = repository

    async def record_quota_event(
        self,
        *,
        decision: QuotaDecision,
        request: QuotaReserveRequest | QuotaSettlementRequest,
        usage: CreditUsage,
    ) -> None:
        if not decision.allowed or decision.idempotent:
            return
        windows = self._decision_windows(decision, request.windows)
        event = UsageLedgerCreate(
            request_id=decision.request_id,
            subscription_id=decision.subscription_id,
            project_id=decision.project_id,
            user_id=request.user_id,
            api_key_id=request.api_key_id,
            model=request.model,
            event_type=self._event_type(decision.event_type),
            credits=decision.credits,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            input_multiplier=request.multipliers.input_multiplier,
            output_multiplier=request.multipliers.output_multiplier,
            cache_read_multiplier=request.multipliers.cache_read_multiplier,
            cache_write_multiplier=request.multipliers.cache_write_multiplier,
            rule_version=request.rule_version,
            window_5h_period_id=self._window_period_id(windows, "5h"),
            window_5h_start=self._window_start(windows, "5h"),
            window_5h_end=self._window_end(windows, "5h"),
            window_week_period_id=self._window_period_id(windows, "week"),
            window_week_start=self._window_start(windows, "week"),
            window_week_end=self._window_end(windows, "week"),
            metadata={
                "balances_before": decision.balances_before,
                "balances_after": decision.balances_after,
                "reservation_state": decision.metadata.get("reservation_state"),
            },
        )
        record = await self.repository.create(event)
        await self.repository.update_subscription_windows(
            subscription_id=record.subscription_id,
            window_5h_start=record.window_5h_start,
            window_week_start=record.window_week_start,
        )

    async def list_events(
        self,
        *,
        subscription_id: str | None = None,
        project_id: str | None = None,
        user_id: str | None = None,
        model: str | None = None,
        event_type: UsageLedgerEventType | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 100,
    ) -> list[UsageLedgerRecord]:
        return await self.repository.list(
            subscription_id=subscription_id,
            project_id=project_id,
            user_id=user_id,
            model=model,
            event_type=event_type.value if event_type else None,
            start_time=start_time,
            end_time=end_time,
            limit=limit,
        )

    async def aggregate_events(
        self,
        *,
        group_by: UsageLedgerGroupBy,
        subscription_id: str | None = None,
        project_id: str | None = None,
        user_id: str | None = None,
        model: str | None = None,
        event_type: UsageLedgerEventType | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 100,
    ) -> list[UsageLedgerAggregateRecord]:
        return await self.repository.aggregate(
            group_by=group_by,
            subscription_id=subscription_id,
            project_id=project_id,
            user_id=user_id,
            model=model,
            event_type=event_type.value if event_type else None,
            start_time=start_time,
            end_time=end_time,
            limit=limit,
        )

    async def manual_adjust(self, data: UsageLedgerManualAdjustRequest) -> UsageLedgerRecord:
        return await self.repository.create(
            UsageLedgerCreate(
                request_id=data.request_id,
                subscription_id=data.subscription_id,
                project_id=data.project_id,
                user_id=data.user_id,
                model=data.model,
                event_type=UsageLedgerEventType.MANUAL_ADJUST,
                credits=data.credits,
                rule_version=data.rule_version,
                metadata=data.metadata,
            )
        )

    def _event_type(self, event_type: QuotaEventType) -> UsageLedgerEventType:
        if event_type == QuotaEventType.EXPIRED:
            return UsageLedgerEventType.EXPIRE
        if event_type == QuotaEventType.COMPENSATED:
            return UsageLedgerEventType.COMPENSATE
        return UsageLedgerEventType(event_type.value)

    def _decision_windows(self, decision: QuotaDecision, fallback: list[QuotaWindow]) -> list[QuotaWindow]:
        windows = decision.metadata.get("windows")
        if not isinstance(windows, list) or not windows:
            return fallback
        parsed: list[QuotaWindow] = []
        for window in windows:
            if isinstance(window, QuotaWindow):
                parsed.append(window)
            elif isinstance(window, dict):
                parsed.append(QuotaWindow.model_validate(window))
        return parsed or fallback

    def _window_period_id(self, windows: list[QuotaWindow], name: str) -> str | None:
        window = self._window(windows, name)
        return window.period_id if window is not None else None

    def _window_start(self, windows: list[QuotaWindow], name: str) -> datetime | None:
        window = self._window(windows, name)
        if window is None or window.anchor_epoch is None or window.starts_on_first_success:
            return None
        return datetime.fromtimestamp(int(window.anchor_epoch), tz=timezone.utc)

    def _window_end(self, windows: list[QuotaWindow], name: str) -> datetime | None:
        window = self._window(windows, name)
        if window is None:
            return None
        return datetime.fromtimestamp(int(window.period_end_epoch), tz=timezone.utc)

    def _window(self, windows: list[QuotaWindow], name: str) -> QuotaWindow | None:
        return next((window for window in windows if window.name == name), None)
