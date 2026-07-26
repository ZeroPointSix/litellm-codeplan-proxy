from __future__ import annotations

import json
from typing import Any

from litellm.product.quotas.models import (
    QuotaDecision,
    QuotaEventType,
    QuotaReserveRequest,
    QuotaSettlementRequest,
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
local ttls_index = limits_index + n
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
  local ttl = tonumber(ARGV[ttls_index + i - 1])
  local current = tonumber(redis.call("INCRBYFLOAT", window_key, credits))
  redis.call("EXPIRE", window_key, ttl)
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
  metadata = {}
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

if not redis.call("GET", KEYS[2]) then
  local missing = {
    allowed = false,
    request_id = ARGV[4],
    subscription_id = ARGV[5],
    project_id = ARGV[6],
    event_type = ARGV[7],
    credits = tonumber(ARGV[1]),
    balances_before = {},
    balances_after = {},
    idempotent = false,
    reason = "Quota reservation not found",
    metadata = {}
  }
  return cjson.encode(missing)
end

local credits = tonumber(ARGV[1])
local reserved = tonumber(ARGV[2])
local event_ttl = tonumber(ARGV[3])
local request_id = ARGV[4]
local subscription_id = ARGV[5]
local project_id = ARGV[6]
local event_type = ARGV[7]
local window_names = cjson.decode(ARGV[8])
local n = tonumber(ARGV[9])
local limits_index = 10
local ttls_index = limits_index + n
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
  local ttl = tonumber(ARGV[ttls_index + i - 1])
  local current = tonumber(redis.call("INCRBYFLOAT", window_key, delta))
  redis.call("EXPIRE", window_key, ttl)
  balances_after[window_names[i]] = limit - current
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
  metadata = {}
}
local encoded = cjson.encode(decision)
redis.call("DEL", KEYS[2])
redis.call("SET", KEYS[1], encoded, "EX", event_ttl)
return encoded
"""


class RedisQuotaStore:
    def __init__(self, redis_client: Any, *, key_prefix: str = "codeplan:quota", event_ttl_seconds: int = 86400):
        self.redis_client = redis_client
        self.key_prefix = key_prefix.rstrip(":")
        self.event_ttl_seconds = event_ttl_seconds

    async def reserve(self, request: QuotaReserveRequest, reserved_credits: float) -> QuotaDecision:
        keys = self._keys(request.subscription_id, request.request_id, QuotaEventType.RESERVE, request.windows)
        args = self._reserve_args(request, reserved_credits)
        payload = await self.redis_client.eval(RESERVE_LUA, len(keys), *keys, *args)
        return self._decision(payload)

    async def settle(self, request: QuotaSettlementRequest, settled_credits: float) -> QuotaDecision:
        keys = self._keys(request.subscription_id, request.request_id, request.event_type, request.windows)
        args = self._settle_args(request, settled_credits)
        payload = await self.redis_client.eval(SETTLE_LUA, len(keys), *keys, *args)
        return self._decision(payload)

    def _keys(self, subscription_id: str, request_id: str, event_type: QuotaEventType, windows: list) -> list[str]:
        return [
            f"{self.key_prefix}:event:{subscription_id}:{request_id}:{event_type.value}",
            f"{self.key_prefix}:reservation:{subscription_id}:{request_id}",
            *[f"{self.key_prefix}:spent:{subscription_id}:{window.name}" for window in windows],
        ]

    def _reserve_args(self, request: QuotaReserveRequest, reserved_credits: float) -> list[str]:
        reservation_payload = json.dumps(
            {
                "request_id": request.request_id,
                "subscription_id": request.subscription_id,
                "project_id": request.project_id,
                "reserved_credits": reserved_credits,
            },
            separators=(",", ":"),
        )
        return [
            str(reserved_credits),
            str(self.event_ttl_seconds),
            reservation_payload,
            request.request_id,
            request.subscription_id,
            request.project_id or "",
            json.dumps([window.name for window in request.windows]),
            str(len(request.windows)),
            *[str(window.limit) for window in request.windows],
            *[str(window.ttl_seconds) for window in request.windows],
        ]

    def _settle_args(self, request: QuotaSettlementRequest, settled_credits: float) -> list[str]:
        return [
            str(settled_credits),
            str(request.reserved_credits),
            str(self.event_ttl_seconds),
            request.request_id,
            request.subscription_id,
            request.project_id or "",
            request.event_type.value,
            json.dumps([window.name for window in request.windows]),
            str(len(request.windows)),
            *[str(window.limit) for window in request.windows],
            *[str(window.ttl_seconds) for window in request.windows],
        ]

    def _decision(self, payload: str | bytes) -> QuotaDecision:
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")
        return QuotaDecision.model_validate_json(payload)
