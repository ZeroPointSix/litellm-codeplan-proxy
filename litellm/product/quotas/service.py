from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Protocol

from litellm.product.quotas.models import (
    CreditMultipliers,
    CreditUsage,
    QuotaDecision,
    QuotaEventType,
    QuotaReserveRequest,
    QuotaSettlementRequest,
    QuotaWindow,
    ReservationState,
)

FIVE_HOURS_SECONDS = 5 * 60 * 60
WEEK_SECONDS = 7 * 24 * 60 * 60
MONTH_SECONDS = 30 * 24 * 60 * 60
PENDING_USAGE_TIMEOUT_SECONDS = 15 * 60


class QuotaExceededError(Exception):
    def __init__(self, decision: QuotaDecision):
        self.decision = decision
        super().__init__(decision.reason or "Code Plan quota exceeded")


class QuotaUnavailableError(Exception):
    pass


class QuotaStore(Protocol):
    async def resolve_windows(self, subscription_id: str, windows: list[QuotaWindow]) -> list[QuotaWindow]: ...

    async def reserve(self, request: QuotaReserveRequest, reserved_credits: float) -> QuotaDecision: ...

    async def settle(self, request: QuotaSettlementRequest, settled_credits: float) -> QuotaDecision: ...


def calculate_credits(usage: CreditUsage, multipliers: CreditMultipliers) -> float:
    return (
        usage.input_tokens * multipliers.input_multiplier
        + usage.output_tokens * multipliers.output_multiplier
        + usage.cache_read_tokens * multipliers.cache_read_multiplier
        + usage.cache_write_tokens * multipliers.cache_write_multiplier
    )


def reserve_usage(input_usage: CreditUsage, default_max_output_tokens: int) -> CreditUsage:
    return CreditUsage(input_tokens=input_usage.input_tokens, output_tokens=default_max_output_tokens)


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


def _parse_epoch(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, datetime):
        dt = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def _subscription_week_anchor_epoch(metadata: Mapping[str, object], *, now_epoch: int) -> int:
    for key in (
        "code_plan_week_anchor_epoch",
        "code_plan_period_start_epoch",
        "code_plan_renewed_at",
        "code_plan_subscription_started_at",
        "code_plan_created_at",
    ):
        parsed = _parse_epoch(metadata.get(key))
        if parsed is not None:
            return parsed
    return now_epoch


def _week_period(anchor_epoch: int, now_epoch: int) -> tuple[str, int, int]:
    if now_epoch < anchor_epoch:
        start = anchor_epoch
    else:
        elapsed = now_epoch - anchor_epoch
        periods = elapsed // WEEK_SECONDS
        start = anchor_epoch + periods * WEEK_SECONDS
    end = start + WEEK_SECONDS
    return f"week:{start}", start, end


def _month_period(anchor_epoch: int, now_epoch: int) -> tuple[str, int, int]:
    if now_epoch < anchor_epoch:
        start = anchor_epoch
    else:
        elapsed = now_epoch - anchor_epoch
        periods = elapsed // MONTH_SECONDS
        start = anchor_epoch + periods * MONTH_SECONDS
    end = start + MONTH_SECONDS
    return f"month:{start}", start, end


def quota_windows_from_metadata(
    metadata: Mapping[str, object],
    *,
    now: datetime | None = None,
) -> list[QuotaWindow]:
    """Build fixed-anchor windows from key/subscription metadata.

    Notion rules:
    - 5h window starts at the first successful call of the current week period
    - weekly window resets every 7 days from subscription start / renew
    - weekly reset also resets the 5h window
    """
    now_dt = now or datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    now_epoch = int(now_dt.timestamp())

    week_anchor = _subscription_week_anchor_epoch(metadata, now_epoch=now_epoch)
    week_period_id, week_start, week_end = _week_period(week_anchor, now_epoch)

    five_h_anchor = _parse_epoch(metadata.get("code_plan_5h_anchor_epoch"))
    if (
        five_h_anchor is not None
        and five_h_anchor >= week_start
        and five_h_anchor < week_end
        and now_epoch < five_h_anchor + FIVE_HOURS_SECONDS
    ):
        five_h_end = five_h_anchor + FIVE_HOURS_SECONDS
        five_h_period_id = f"5h:{five_h_anchor}"
        five_h_starts_on_first_success = False
        five_h_ttl = max(1, five_h_end - now_epoch)
        five_h_anchor_value: int | None = five_h_anchor
    else:
        # Unopened 5h window: period id is scoped to the current week so a weekly
        # reset cannot reuse prior 5h spend. TTL stretches to week end until the
        # first successful call anchors the real 5h window.
        five_h_period_id = f"5h-open:{week_period_id}"
        five_h_end = week_end
        five_h_starts_on_first_success = True
        five_h_ttl = max(1, five_h_end - now_epoch)
        five_h_anchor_value = None

    windows = [
        QuotaWindow(
            name="5h",
            limit=float(metadata["code_plan_quota_5h"]),
            ttl_seconds=five_h_ttl,
            period_id=five_h_period_id,
            period_end_epoch=five_h_end,
            anchor_epoch=five_h_anchor_value,
            starts_on_first_success=five_h_starts_on_first_success,
        ),
        QuotaWindow(
            name="week",
            limit=float(metadata["code_plan_quota_weekly"]),
            ttl_seconds=max(1, week_end - now_epoch),
            period_id=week_period_id,
            period_end_epoch=week_end,
            anchor_epoch=week_start,
            starts_on_first_success=False,
        ),
    ]

    monthly_limit = metadata.get("code_plan_quota_monthly")
    if monthly_limit is not None:
        month_anchor = _parse_epoch(metadata.get("code_plan_month_anchor_epoch")) or week_anchor
        month_period_id, month_start, month_end = _month_period(month_anchor, now_epoch)
        windows.append(
            QuotaWindow(
                name="month",
                limit=float(monthly_limit),
                ttl_seconds=max(1, month_end - now_epoch),
                period_id=month_period_id,
                period_end_epoch=month_end,
                anchor_epoch=month_start,
                starts_on_first_success=False,
            )
        )
    return windows


def anchored_windows_after_first_success(
    windows: list[QuotaWindow],
    *,
    success_epoch: int | None = None,
) -> list[QuotaWindow]:
    """Materialize the 5h fixed window at first successful settlement."""
    now_epoch = success_epoch if success_epoch is not None else int(datetime.now(timezone.utc).timestamp())
    updated: list[QuotaWindow] = []
    for window in windows:
        if window.name == "5h" and window.starts_on_first_success:
            end = now_epoch + FIVE_HOURS_SECONDS
            updated.append(
                QuotaWindow(
                    name=window.name,
                    limit=window.limit,
                    ttl_seconds=FIVE_HOURS_SECONDS,
                    period_id=f"5h:{now_epoch}",
                    period_end_epoch=end,
                    anchor_epoch=now_epoch,
                    starts_on_first_success=False,
                )
            )
        else:
            updated.append(window)
    return updated


def apply_persisted_5h_anchor(windows: list[QuotaWindow], anchor_epoch: int | None) -> list[QuotaWindow]:
    if anchor_epoch is None:
        return windows
    updated: list[QuotaWindow] = []
    for window in windows:
        if window.name != "5h":
            updated.append(window)
            continue
        end = anchor_epoch + FIVE_HOURS_SECONDS
        updated.append(
            QuotaWindow(
                name=window.name,
                limit=window.limit,
                ttl_seconds=max(1, end - int(datetime.now(timezone.utc).timestamp())),
                period_id=f"5h:{anchor_epoch}",
                period_end_epoch=end,
                anchor_epoch=anchor_epoch,
                starts_on_first_success=False,
            )
        )
    return updated


def _now_epoch() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _event_state(event_type: QuotaEventType, settled_credits: float) -> ReservationState:
    if event_type == QuotaEventType.PENDING_USAGE:
        return ReservationState.PENDING_USAGE
    if event_type == QuotaEventType.EXPIRED:
        return ReservationState.EXPIRED
    if event_type == QuotaEventType.COMPENSATED:
        return ReservationState.COMPENSATED
    if event_type == QuotaEventType.RELEASE and settled_credits == 0:
        return ReservationState.RELEASED
    return ReservationState.SETTLED


class InMemoryQuotaStore:
    def __init__(self) -> None:
        self.spent: dict[tuple[str, str, str], float] = {}
        self.events: dict[tuple[str, str, QuotaEventType], QuotaDecision] = {}
        self.reservations: dict[tuple[str, str], dict[str, object]] = {}
        self.anchors_5h: dict[tuple[str, str], int] = {}

    async def resolve_windows(self, subscription_id: str, windows: list[QuotaWindow]) -> list[QuotaWindow]:
        week = next((window for window in windows if window.name == "week"), None)
        if week is None:
            return windows
        anchor = self.anchors_5h.get((subscription_id, week.period_id))
        return apply_persisted_5h_anchor(windows, anchor)

    async def reserve(self, request: QuotaReserveRequest, reserved_credits: float) -> QuotaDecision:
        event_key = (request.subscription_id, request.request_id, QuotaEventType.RESERVE)
        if event_key in self.events:
            return self.events[event_key].model_copy(update={"idempotent": True})

        balances_before = self._balances_before(request.subscription_id, request.windows)
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
            key = (request.subscription_id, window.name, window.period_id)
            self.spent[key] = self.spent.get(key, 0.0) + reserved_credits
        balances_after = self._balances_before(request.subscription_id, request.windows)
        reservation_key = (request.subscription_id, request.request_id)
        self.reservations[reservation_key] = {
            "request_id": request.request_id,
            "subscription_id": request.subscription_id,
            "project_id": request.project_id,
            "reserved_credits": reserved_credits,
            "state": ReservationState.RESERVED.value,
            "windows": [window.model_dump() for window in request.windows],
            "created_at_epoch": _now_epoch(),
        }
        decision = QuotaDecision(
            allowed=True,
            request_id=request.request_id,
            subscription_id=request.subscription_id,
            project_id=request.project_id,
            event_type=QuotaEventType.RESERVE,
            credits=reserved_credits,
            balances_before=balances_before,
            balances_after=balances_after,
            metadata={"reservation_state": ReservationState.RESERVED.value},
        )
        self.events[event_key] = decision
        return decision

    async def settle(self, request: QuotaSettlementRequest, settled_credits: float) -> QuotaDecision:
        event_key = (request.subscription_id, request.request_id, request.event_type)
        if event_key in self.events:
            return self.events[event_key].model_copy(update={"idempotent": True})

        reservation_key = (request.subscription_id, request.request_id)
        reservation = self.reservations.get(reservation_key)
        if reservation is None:
            return QuotaDecision(
                allowed=False,
                request_id=request.request_id,
                subscription_id=request.subscription_id,
                project_id=request.project_id,
                event_type=request.event_type,
                credits=settled_credits,
                balances_before={},
                balances_after={},
                reason="Quota reservation not found",
            )

        reserved_raw = reservation.get("reserved_credits", 0.0)
        reserved_credits = float(reserved_raw) if isinstance(reserved_raw, (int, float, str)) else 0.0
        windows = request.windows
        if request.event_type == QuotaEventType.SETTLE and settled_credits > 0 and request.anchor_first_success:
            windows = anchored_windows_after_first_success(windows)
            reservation["windows"] = [window.model_dump() for window in windows]
            week = next((window for window in windows if window.name == "week"), None)
            five = next((window for window in windows if window.name == "5h"), None)
            if week is not None and five is not None and five.anchor_epoch is not None:
                self.anchors_5h.setdefault((request.subscription_id, week.period_id), int(five.anchor_epoch))

        balances_before = self._balances_before(request.subscription_id, windows)
        delta = settled_credits - reserved_credits
        for window in windows:
            key = (request.subscription_id, window.name, window.period_id)
            # Move spend from the pre-anchor open period into the fixed 5h period when needed.
            if window.name == "5h" and not window.starts_on_first_success:
                open_keys = [
                    k
                    for k in self.spent
                    if k[0] == request.subscription_id and k[1] == "5h" and str(k[2]).startswith("5h-open:")
                ]
                open_spent = sum(self.spent.get(k, 0.0) for k in open_keys)
                if open_spent and key not in self.spent:
                    self.spent[key] = open_spent
                    for open_key in open_keys:
                        self.spent.pop(open_key, None)
            self.spent[key] = self.spent.get(key, 0.0) + delta

        balances_after = self._balances_before(request.subscription_id, windows)
        state = _event_state(request.event_type, settled_credits)
        if request.event_type == QuotaEventType.PENDING_USAGE:
            reservation["state"] = state.value
            reservation["pending_since_epoch"] = _now_epoch()
        elif request.event_type == QuotaEventType.EXPIRED:
            reservation["state"] = state.value
            reservation["expired_at_epoch"] = _now_epoch()
        else:
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
            metadata={
                "reservation_state": state.value,
                "windows": [window.model_dump() for window in windows],
            },
        )
        self.events[event_key] = decision
        return decision

    async def expire_pending_usage(
        self,
        subscription_id: str | None = None,
        *,
        older_than_epoch: int | None = None,
    ) -> list[QuotaDecision]:
        cutoff = older_than_epoch if older_than_epoch is not None else _now_epoch() - PENDING_USAGE_TIMEOUT_SECONDS
        decisions: list[QuotaDecision] = []
        for (sub_id, request_id), reservation in list(self.reservations.items()):
            if subscription_id is not None and sub_id != subscription_id:
                continue
            if reservation.get("state") != ReservationState.PENDING_USAGE.value:
                continue
            pending_since = int(reservation.get("pending_since_epoch") or reservation.get("created_at_epoch") or 0)
            if pending_since > cutoff:
                continue
            windows = [QuotaWindow.model_validate(window) for window in reservation.get("windows", [])]
            reserved_credits = float(reservation.get("reserved_credits", 0.0))
            decisions.append(
                await self.settle(
                    QuotaSettlementRequest(
                        request_id=str(request_id),
                        subscription_id=str(sub_id),
                        project_id=reservation.get("project_id")
                        if isinstance(reservation.get("project_id"), str)
                        else None,
                        actual_usage=CreditUsage(),
                        multipliers=CreditMultipliers(),
                        windows=windows,
                        reserved_credits=reserved_credits,
                        event_type=QuotaEventType.EXPIRED,
                        anchor_first_success=False,
                    ),
                    reserved_credits,
                )
            )
        return decisions

    async def compensate_expired_usage(self, subscription_id: str | None = None) -> list[QuotaDecision]:
        decisions: list[QuotaDecision] = []
        for (sub_id, request_id), reservation in list(self.reservations.items()):
            if subscription_id is not None and sub_id != subscription_id:
                continue
            if reservation.get("state") != ReservationState.EXPIRED.value:
                continue
            windows = [QuotaWindow.model_validate(window) for window in reservation.get("windows", [])]
            reserved_credits = float(reservation.get("reserved_credits", 0.0))
            decisions.append(
                await self.settle(
                    QuotaSettlementRequest(
                        request_id=str(request_id),
                        subscription_id=str(sub_id),
                        project_id=reservation.get("project_id")
                        if isinstance(reservation.get("project_id"), str)
                        else None,
                        actual_usage=CreditUsage(),
                        multipliers=CreditMultipliers(),
                        windows=windows,
                        reserved_credits=reserved_credits,
                        event_type=QuotaEventType.COMPENSATED,
                        anchor_first_success=False,
                    ),
                    0.0,
                )
            )
        return decisions

    def _balances_before(self, subscription_id: str, windows: list[QuotaWindow]) -> dict[str, float]:
        balances: dict[str, float] = {}
        for window in windows:
            spent = self.spent.get((subscription_id, window.name, window.period_id), 0.0)
            balances[window.name] = window.limit - spent
        return balances


class QuotaService:
    def __init__(self, store: QuotaStore):
        self.store = store

    async def _with_resolved_windows(self, subscription_id: str, windows: list[QuotaWindow]) -> list[QuotaWindow]:
        resolve = getattr(self.store, "resolve_windows", None)
        if resolve is None:
            return windows
        return await resolve(subscription_id, windows)

    async def reserve(self, request: QuotaReserveRequest) -> QuotaDecision:
        resolved_windows = await self._with_resolved_windows(request.subscription_id, request.windows)
        request = request.model_copy(update={"windows": resolved_windows})
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
        resolved_windows = await self._with_resolved_windows(request.subscription_id, request.windows)
        request = request.model_copy(update={"windows": resolved_windows})
        if request.event_type in {QuotaEventType.PENDING_USAGE, QuotaEventType.EXPIRED}:
            # Hold currently reserved credits until usage is confirmed or compensation runs.
            credits = float(request.reserved_credits)
        elif request.event_type == QuotaEventType.COMPENSATED:
            credits = 0.0
        else:
            credits = calculate_credits(request.actual_usage, request.multipliers)
        try:
            return await self.store.settle(request, credits)
        except Exception as exc:
            raise QuotaUnavailableError("Code Plan quota store unavailable") from exc

    async def expire_pending_usage(
        self,
        subscription_id: str | None = None,
        *,
        older_than_epoch: int | None = None,
    ) -> list[QuotaDecision]:
        expire = getattr(self.store, "expire_pending_usage", None)
        if expire is None:
            return []
        cutoff = older_than_epoch if older_than_epoch is not None else _now_epoch() - PENDING_USAGE_TIMEOUT_SECONDS
        try:
            return await expire(subscription_id=subscription_id, older_than_epoch=cutoff)
        except Exception as exc:
            raise QuotaUnavailableError("Code Plan quota store unavailable") from exc

    async def compensate_expired_usage(self, subscription_id: str | None = None) -> list[QuotaDecision]:
        compensate = getattr(self.store, "compensate_expired_usage", None)
        if compensate is None:
            return []
        try:
            return await compensate(subscription_id=subscription_id)
        except Exception as exc:
            raise QuotaUnavailableError("Code Plan quota store unavailable") from exc

    async def compensate_expired_pending_usage(
        self,
        subscription_id: str | None = None,
        *,
        older_than_epoch: int | None = None,
    ) -> list[QuotaDecision]:
        expired = await self.expire_pending_usage(subscription_id=subscription_id, older_than_epoch=older_than_epoch)
        compensated = await self.compensate_expired_usage(subscription_id=subscription_id)
        return [*expired, *compensated]
