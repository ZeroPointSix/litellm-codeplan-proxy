from __future__ import annotations

import json

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
from litellm.product.quotas.service import (
    FIVE_HOURS_SECONDS,
    PENDING_USAGE_TIMEOUT_SECONDS,
    anchored_windows_after_first_success,
)

RESERVE_LUA = """
local event_value = redis.call("GET", KEYS[1])
if event_value then
  local decision = cjson.decode(event_value)
  decision["idempotent"] = true
  return cjson.encode(decision)
end

local credits = tonumber(ARGV[1])
local event_ttl = tonumber(ARGV[2])
local reservation_payload = ARGV[3]
local request_id = ARGV[4]
local subscription_id = ARGV[5]
local project_id = ARGV[6]
local window_names = cjson.decode(ARGV[7])
local n = tonumber(ARGV[8])
local limits_index = 9
local ends_index = limits_index + n
local balances_before = {}
local balances_after = {}
local allowed = true

for i = 1, n do
  local window_key = KEYS[i + 2]
  local current = tonumber(redis.call("GET", window_key) or "0")
  local limit = tonumber(ARGV[limits_index + i - 1])
  local balance = limit - current
  balances_before[window_names[i]] = balance
  if balance <= 0 then
    allowed = false
  end
end

if not allowed then
  local decision = {
    allowed = false,
    request_id = request_id,
    subscription_id = subscription_id,
    project_id = project_id,
    event_type = "reserve",
    credits = credits,
    balances_before = balances_before,
    balances_after = balances_before,
    idempotent = false,
    reason = "Code Plan quota exhausted",
    metadata = {}
  }
  return cjson.encode(decision)
end

for i = 1, n do
  local window_key = KEYS[i + 2]
  local limit = tonumber(ARGV[limits_index + i - 1])
  local period_end = tonumber(ARGV[ends_index + i - 1])
  local current = tonumber(redis.call("INCRBYFLOAT", window_key, credits))
  redis.call("EXPIREAT", window_key, period_end)
  balances_after[window_names[i]] = limit - current
end

local decision = {
  allowed = true,
  request_id = request_id,
  subscription_id = subscription_id,
  project_id = project_id,
  event_type = "reserve",
  credits = credits,
  balances_before = balances_before,
  balances_after = balances_after,
  idempotent = false,
  reason = cjson.null,
  metadata = {reservation_state = "RESERVED"}
}
local encoded = cjson.encode(decision)
redis.call("SET", KEYS[2], reservation_payload, "EX", event_ttl)
redis.call("SET", KEYS[1], encoded, "EX", event_ttl)
return encoded
"""

SETTLE_LUA = """
local event_value = redis.call("GET", KEYS[1])
if event_value then
  local decision = cjson.decode(event_value)
  decision["idempotent"] = true
  return cjson.encode(decision)
end

local reservation_raw = redis.call("GET", KEYS[2])
if not reservation_raw then
  local missing = {
    allowed = false,
    request_id = ARGV[3],
    subscription_id = ARGV[4],
    project_id = ARGV[5],
    event_type = ARGV[6],
    credits = tonumber(ARGV[1]),
    balances_before = {},
    balances_after = {},
    idempotent = false,
    reason = "Quota reservation not found",
    metadata = {}
  }
  return cjson.encode(missing)
end

local reservation = cjson.decode(reservation_raw)
local credits = tonumber(ARGV[1])
local reserved = tonumber(reservation["reserved_credits"])
local event_ttl = tonumber(ARGV[2])
local request_id = ARGV[3]
local subscription_id = ARGV[4]
local project_id = ARGV[5]
local event_type = ARGV[6]
local window_names = cjson.decode(ARGV[7])
local n = tonumber(ARGV[8])
local limits_index = 9
local ends_index = limits_index + n
local open_keys = cjson.decode(ARGV[ends_index + n] or "[]")
local now_epoch = tonumber(ARGV[ends_index + n + 1])
local delta = credits - reserved
local balances_before = {}
local balances_after = {}

for i = 1, n do
  local window_key = KEYS[i + 2]
  local current = tonumber(redis.call("GET", window_key) or "0")
  local limit = tonumber(ARGV[limits_index + i - 1])
  balances_before[window_names[i]] = limit - current
end

for i = 1, n do
  local window_key = KEYS[i + 2]
  local limit = tonumber(ARGV[limits_index + i - 1])
  local period_end = tonumber(ARGV[ends_index + i - 1])
  local open_key = open_keys[i]
  if open_key and open_key ~= "" and open_key ~= window_key then
    local open_spent = tonumber(redis.call("GET", open_key) or "0")
    if open_spent ~= 0 then
      local existing = tonumber(redis.call("GET", window_key) or "0")
      if existing == 0 then
        redis.call("SET", window_key, open_spent)
      else
        redis.call("INCRBYFLOAT", window_key, open_spent)
      end
      redis.call("DEL", open_key)
    end
  end
  local current = tonumber(redis.call("INCRBYFLOAT", window_key, delta))
  redis.call("EXPIREAT", window_key, period_end)
  balances_after[window_names[i]] = limit - current
end

local reservation_state = "SETTLED"
if event_type == "pending_usage" then
  reservation_state = "PENDING_USAGE"
elseif event_type == "expired" then
  reservation_state = "EXPIRED"
elseif event_type == "compensated" then
  reservation_state = "COMPENSATED"
elseif event_type == "release" and credits == 0 then
  reservation_state = "RELEASED"
elseif event_type == "release" then
  reservation_state = "SETTLED"
end

local decision = {
  allowed = true,
  request_id = request_id,
  subscription_id = subscription_id,
  project_id = project_id,
  event_type = event_type,
  credits = credits,
  balances_before = balances_before,
  balances_after = balances_after,
  idempotent = false,
  reason = cjson.null,
  metadata = {reservation_state = reservation_state}
}
local encoded = cjson.encode(decision)
if event_type == "pending_usage" then
  reservation["state"] = reservation_state
  reservation["pending_since_epoch"] = now_epoch
  redis.call("SET", KEYS[2], cjson.encode(reservation), "EX", event_ttl)
elseif event_type == "expired" then
  reservation["state"] = reservation_state
  reservation["expired_at_epoch"] = now_epoch
  redis.call("SET", KEYS[2], cjson.encode(reservation), "EX", event_ttl)
else
  redis.call("DEL", KEYS[2])
end
redis.call("SET", KEYS[1], encoded, "EX", event_ttl)
return encoded
"""


class RedisQuotaStore:
    def __init__(self, redis_client: object, *, key_prefix: str = "codeplan:quota", event_ttl_seconds: int = 86400):
        self.redis_client = redis_client
        self.key_prefix = key_prefix.rstrip(":")
        self.event_ttl_seconds = event_ttl_seconds

    async def resolve_windows(self, subscription_id: str, windows: list[QuotaWindow]) -> list[QuotaWindow]:
        from litellm.product.quotas.service import apply_persisted_5h_anchor

        week = next((window for window in windows if window.name == "week"), None)
        if week is None:
            return windows
        key = f"{self.key_prefix}:anchor5h:{subscription_id}:{week.period_id}"
        raw = await self.redis_client.get(key)
        if raw is None:
            return windows
        try:
            anchor = int(raw)
        except (TypeError, ValueError):
            return windows
        return apply_persisted_5h_anchor(windows, anchor)

    async def reserve(self, request: QuotaReserveRequest, reserved_credits: float) -> QuotaDecision:
        keys = self._keys(request.subscription_id, request.request_id, QuotaEventType.RESERVE, request.windows)
        args = self._reserve_args(request, reserved_credits)
        payload = await self.redis_client.eval(RESERVE_LUA, len(keys), *keys, *args)
        return self._decision(payload)

    async def settle(self, request: QuotaSettlementRequest, settled_credits: float) -> QuotaDecision:
        windows = list(request.windows)
        open_period_keys: list[str] = [""] * len(windows)
        if request.event_type == QuotaEventType.SETTLE and settled_credits > 0 and request.anchor_first_success:
            anchored = anchored_windows_after_first_success(windows)
            open_period_keys = []
            for before, after in zip(windows, anchored, strict=True):
                if before.period_id != after.period_id:
                    open_period_keys.append(self._spent_key(request.subscription_id, before.name, before.period_id))
                else:
                    open_period_keys.append("")
            windows = anchored
        keys = self._keys(request.subscription_id, request.request_id, request.event_type, windows)
        args = self._settle_args(request, settled_credits, windows, open_period_keys)
        payload = await self.redis_client.eval(SETTLE_LUA, len(keys), *keys, *args)
        decision = self._decision(payload)
        if decision.allowed and decision.metadata is not None:
            # Expose anchored windows to callers (e.g. hook metadata freeze).
            decision.metadata = {
                **decision.metadata,
                "windows": [window.model_dump() for window in windows],
            }
            if request.event_type == QuotaEventType.SETTLE and settled_credits > 0 and request.anchor_first_success:
                await self._persist_5h_anchor(request.subscription_id, windows)
        return decision

    async def expire_pending_usage(
        self,
        subscription_id: str | None = None,
        *,
        older_than_epoch: int | None = None,
    ) -> list[QuotaDecision]:
        cutoff = older_than_epoch if older_than_epoch is not None else self._now_epoch() - PENDING_USAGE_TIMEOUT_SECONDS
        decisions: list[QuotaDecision] = []
        async for _key, reservation in self._iter_reservations(subscription_id):
            if reservation.get("state") != ReservationState.PENDING_USAGE.value:
                continue
            pending_since = int(reservation.get("pending_since_epoch") or reservation.get("created_at_epoch") or 0)
            if pending_since > cutoff:
                continue
            windows = [QuotaWindow.model_validate(window) for window in reservation.get("windows", [])]
            if not windows:
                continue
            reserved_credits = float(reservation.get("reserved_credits", 0.0))
            decisions.append(
                await self.settle(
                    QuotaSettlementRequest(
                        request_id=str(reservation["request_id"]),
                        subscription_id=str(reservation["subscription_id"]),
                        project_id=reservation.get("project_id") if isinstance(reservation.get("project_id"), str) else None,
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
        async for _key, reservation in self._iter_reservations(subscription_id):
            if reservation.get("state") != ReservationState.EXPIRED.value:
                continue
            windows = [QuotaWindow.model_validate(window) for window in reservation.get("windows", [])]
            if not windows:
                continue
            reserved_credits = float(reservation.get("reserved_credits", 0.0))
            decisions.append(
                await self.settle(
                    QuotaSettlementRequest(
                        request_id=str(reservation["request_id"]),
                        subscription_id=str(reservation["subscription_id"]),
                        project_id=reservation.get("project_id") if isinstance(reservation.get("project_id"), str) else None,
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

    async def _persist_5h_anchor(self, subscription_id: str, windows: list[QuotaWindow]) -> None:
        week = next((window for window in windows if window.name == "week"), None)
        five = next((window for window in windows if window.name == "5h"), None)
        if week is None or five is None or five.anchor_epoch is None:
            return
        key = f"{self.key_prefix}:anchor5h:{subscription_id}:{week.period_id}"
        # SET NX keeps the first successful call as the fixed 5h anchor.
        await self.redis_client.set(key, str(int(five.anchor_epoch)), nx=True, exat=int(week.period_end_epoch))

    async def _iter_reservations(self, subscription_id: str | None = None):
        pattern = f"{self.key_prefix}:reservation:{subscription_id}:*" if subscription_id else f"{self.key_prefix}:reservation:*"
        scan_iter = getattr(self.redis_client, "scan_iter", None)
        if scan_iter is not None:
            async for key in scan_iter(match=pattern):
                raw = await self.redis_client.get(key)
                if raw is None:
                    continue
                decoded = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
                try:
                    yield key, json.loads(decoded)
                except json.JSONDecodeError:
                    continue
            return
        keys = await self.redis_client.keys(pattern)
        for key in keys:
            raw = await self.redis_client.get(key)
            if raw is None:
                continue
            decoded = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
            try:
                yield key, json.loads(decoded)
            except json.JSONDecodeError:
                continue

    def _ttl_for_windows(self, windows: list[QuotaWindow]) -> int:
        if not windows:
            return self.event_ttl_seconds
        # Keep reservation/event records at least as long as the longest remaining window.
        now_epoch = self._now_epoch()
        max_remaining = max(max(1, window.period_end_epoch - now_epoch, window.ttl_seconds) for window in windows)
        return max(self.event_ttl_seconds, max_remaining, FIVE_HOURS_SECONDS)

    def _now_epoch(self) -> int:
        import time

        return int(time.time())

    def _spent_key(self, subscription_id: str, window_name: str, period_id: str) -> str:
        return f"{self.key_prefix}:spent:{subscription_id}:{window_name}:{period_id}"

    def _keys(
        self,
        subscription_id: str,
        request_id: str,
        event_type: QuotaEventType,
        windows: list[QuotaWindow],
    ) -> list[str]:
        return [
            f"{self.key_prefix}:event:{subscription_id}:{request_id}:{event_type.value}",
            f"{self.key_prefix}:reservation:{subscription_id}:{request_id}",
            *[self._spent_key(subscription_id, window.name, window.period_id) for window in windows],
        ]

    def _reserve_args(self, request: QuotaReserveRequest, reserved_credits: float) -> list[str]:
        reservation_payload = json.dumps(
            {
                "request_id": request.request_id,
                "subscription_id": request.subscription_id,
                "project_id": request.project_id,
                "reserved_credits": reserved_credits,
                "state": ReservationState.RESERVED.value,
                "windows": [window.model_dump() for window in request.windows],
                "created_at_epoch": self._now_epoch(),
            },
            separators=(",", ":"),
        )
        return [
            str(reserved_credits),
            str(self._ttl_for_windows(request.windows)),
            reservation_payload,
            request.request_id,
            request.subscription_id,
            request.project_id or "",
            json.dumps([window.name for window in request.windows]),
            str(len(request.windows)),
            *[str(window.limit) for window in request.windows],
            *[str(window.period_end_epoch) for window in request.windows],
        ]

    def _settle_args(
        self,
        request: QuotaSettlementRequest,
        settled_credits: float,
        windows: list[QuotaWindow],
        open_period_keys: list[str],
    ) -> list[str]:
        return [
            str(settled_credits),
            str(self._ttl_for_windows(windows)),
            request.request_id,
            request.subscription_id,
            request.project_id or "",
            request.event_type.value,
            json.dumps([window.name for window in windows]),
            str(len(windows)),
            *[str(window.limit) for window in windows],
            *[str(window.period_end_epoch) for window in windows],
            json.dumps(open_period_keys),
            str(self._now_epoch()),
        ]

    def _decision(self, payload: str | bytes) -> QuotaDecision:
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")
        return QuotaDecision.model_validate_json(payload)
