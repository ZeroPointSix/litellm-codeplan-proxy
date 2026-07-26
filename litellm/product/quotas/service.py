from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from litellm.product.quotas.models import (
    CreditMultipliers,
    CreditUsage,
    QuotaDecision,
    QuotaEventType,
    QuotaReserveRequest,
    QuotaSettlementRequest,
    QuotaWindow,
)

FIVE_HOURS_SECONDS = 5 * 60 * 60
WEEK_SECONDS = 7 * 24 * 60 * 60
MONTH_SECONDS = 30 * 24 * 60 * 60


class QuotaExceededError(Exception):
    def __init__(self, decision: QuotaDecision):
        self.decision = decision
        super().__init__(decision.reason or "Code Plan quota exceeded")


class QuotaUnavailableError(Exception):
    pass


class QuotaStore(Protocol):
    async def reserve(
        self,
        request: QuotaReserveRequest,
        reserved_credits: float,
    ) -> QuotaDecision:
        ...

    async def settle(
        self,
        request: QuotaSettlementRequest,
        settled_credits: float,
    ) -> QuotaDecision:
        ...


def calculate_credits(usage: CreditUsage, multipliers: CreditMultipliers) -> float:
    return (
        usage.input_tokens * multipliers.input_multiplier
        + usage.output_tokens * multipliers.output_multiplier
        + usage.cache_read_tokens * multipliers.cache_read_multiplier
        + usage.cache_write_tokens * multipliers.cache_write_multiplier
    )


def reserve_usage(
    input_usage: CreditUsage,
    default_max_output_tokens: int,
) -> CreditUsage:
    return CreditUsage(
        input_tokens=input_usage.input_tokens,
        output_tokens=default_max_output_tokens,
    )


def settlement_usage_for_failure(
    reserved_input_usage: CreditUsage,
    actual_usage: CreditUsage | None = None,
    *,
    status_code: int | None = None,
    gateway_rejected: bool = False,
) -> CreditUsage:
    if gateway_rejected:
        return CreditUsage()
    if status_code is not None and status_code >= 500:
        return CreditUsage(
            input_tokens=reserved_input_usage.input_tokens,
            cache_read_tokens=(actual_usage.cache_read_tokens if actual_usage else 0),
            cache_write_tokens=(actual_usage.cache_write_tokens if actual_usage else 0),
        )
    return CreditUsage()


def quota_windows_from_metadata(metadata: Mapping[str, object]) -> list[QuotaWindow]:
    windows = [
        QuotaWindow(
            name="5h",
            limit=float(metadata["code_plan_quota_5h"]),
            ttl_seconds=FIVE_HOURS_SECONDS,
        ),
        QuotaWindow(
            name="week",
            limit=float(metadata["code_plan_quota_weekly"]),
            ttl_seconds=WEEK_SECONDS,
        ),
    ]
    monthly_limit = metadata.get("code_plan_quota_monthly")
    if monthly_limit is not None:
        windows.append(
            QuotaWindow(
                name="month",
                limit=float(monthly_limit),
                ttl_seconds=MONTH_SECONDS,
            )
        )
    return windows


class InMemoryQuotaStore:
    def __init__(self) -> None:
        self.spent: dict[tuple[str, str], float] = {}
        self.events: dict[tuple[str, str, QuotaEventType], QuotaDecision] = {}
        self.reservations: dict[tuple[str, str], float] = {}

    async def reserve(
        self,
        request: QuotaReserveRequest,
        reserved_credits: float,
    ) -> QuotaDecision:
        event_key = (
            request.subscription_id,
            request.request_id,
            QuotaEventType.RESERVE,
        )
        if event_key in self.events:
            return self.events[event_key].model_copy(update={"idempotent": True})

        balances_before = self._balances_before(
            request.subscription_id,
            request.windows,
        )
        if any(balance <= 0 for balance in balances_before.values()):
            decision = QuotaDecision(
                allowed=False,
                request_id=request.request_id,
                subscription_id=request.subscription_id,
                project_id=request.project_id,
                event_type=QuotaEventType.RESERVE,
                credits=reserved_credits,
                balances_before=balances_before,
                balances_after=balances_before,
                reason="Code Plan quota exhausted",
            )
            return decision

        for window in request.windows:
            self.spent[(request.subscription_id, window.name)] = (
                self.spent.get((request.subscription_id, window.name), 0.0)
                + reserved_credits
            )
        balances_after = self._balances_before(
            request.subscription_id,
            request.windows,
        )
        reservation_key = (request.subscription_id, request.request_id)
        self.reservations[reservation_key] = reserved_credits
        decision = QuotaDecision(
            allowed=True,
            request_id=request.request_id,
            subscription_id=request.subscription_id,
            project_id=request.project_id,
            event_type=QuotaEventType.RESERVE,
            credits=reserved_credits,
            balances_before=balances_before,
            balances_after=balances_after,
        )
        self.events[event_key] = decision
        return decision

    async def settle(
        self,
        request: QuotaSettlementRequest,
        settled_credits: float,
    ) -> QuotaDecision:
        event_key = (
            request.subscription_id,
            request.request_id,
            request.event_type,
        )
        if event_key in self.events:
            return self.events[event_key].model_copy(update={"idempotent": True})

        reservation_key = (request.subscription_id, request.request_id)
        reserved_credits = self.reservations.get(
            reservation_key,
            request.reserved_credits,
        )
        balances_before = self._balances_before(
            request.subscription_id,
            request.windows,
        )
        delta = settled_credits - reserved_credits
        for window in request.windows:
            self.spent[(request.subscription_id, window.name)] = (
                self.spent.get((request.subscription_id, window.name), 0.0) + delta
            )
        balances_after = self._balances_before(
            request.subscription_id,
            request.windows,
        )
        self.reservations.pop(reservation_key, None)
        decision = QuotaDecision(
            allowed=True,
            request_id=request.request_id,
            subscription_id=request.subscription_id,
            project_id=request.project_id,
            event_type=request.event_type,
            credits=settled_credits,
            balances_before=balances_before,
            balances_after=balances_after,
        )
        self.events[event_key] = decision
        return decision

    def _balances_before(
        self,
        subscription_id: str,
        windows: list[QuotaWindow],
    ) -> dict[str, float]:
        return {window.name: window.limit - self.spent.get((subscription_id, window.name), 0.0) for window in windows}


class QuotaService:
    def __init__(self, store: QuotaStore):
        self.store = store

    async def reserve(self, request: QuotaReserveRequest) -> QuotaDecision:
        credits = calculate_credits(
            reserve_usage(request.input_usage, request.default_max_output_tokens),
            request.multipliers,
        )
        try:
            decision = await self.store.reserve(request, credits)
        except Exception as exc:
            raise QuotaUnavailableError("Code Plan quota store unavailable") from exc
        if not decision.allowed:
            raise QuotaExceededError(decision)
        return decision

    async def settle(self, request: QuotaSettlementRequest) -> QuotaDecision:
        credits = calculate_credits(request.actual_usage, request.multipliers)
        try:
            return await self.store.settle(request, credits)
        except Exception as exc:
            raise QuotaUnavailableError("Code Plan quota store unavailable") from exc
