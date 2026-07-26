from __future__ import annotations

import os
from typing import Any
from uuid import uuid4

import litellm
from fastapi import HTTPException, status

from litellm.caching.caching import DualCache
from litellm.integrations.custom_logger import CustomLogger
from litellm.product.quotas.models import (
    CreditMultipliers,
    CreditUsage,
    QuotaEventType,
    QuotaReserveRequest,
    QuotaSettlementRequest,
)
from litellm.product.quotas.redis_store import RedisQuotaStore
from litellm.product.quotas.service import (
    QuotaExceededError,
    QuotaService,
    QuotaUnavailableError,
    quota_windows_from_metadata,
    settlement_usage_for_failure,
)
from litellm.proxy._types import UserAPIKeyAuth
from litellm.types.utils import CallTypesLiteral

CODE_PLAN_QUOTA_RESERVATION_METADATA_KEY = "code_plan_quota_reservation"
CODE_PLAN_REQUEST_ID_METADATA_KEY = "code_plan_request_id"


class _PROXY_CodePlanQuotaHandler(CustomLogger):
    def __init__(self, quota_service: QuotaService | None = None):
        self._quota_service = quota_service

    async def async_pre_call_hook(
        self,
        user_api_key_dict: UserAPIKeyAuth,
        cache: DualCache,
        data: dict,
        call_type: CallTypesLiteral,
    ) -> dict | None:
        metadata = self._key_metadata(user_api_key_dict)
        if not self._is_code_plan_key(metadata):
            return None

        request_metadata = data.setdefault("metadata", {})
        request_id = self._request_id(request_metadata)
        input_usage = self._input_usage_from_data(data)
        reserve_request = QuotaReserveRequest(
            request_id=request_id,
            subscription_id=str(metadata["code_plan_subscription_id"]),
            project_id=self._optional_str(metadata.get("code_plan_project_id")),
            input_usage=input_usage,
            multipliers=self._multipliers(metadata),
            windows=quota_windows_from_metadata(metadata),
            default_max_output_tokens=self._int(metadata.get("code_plan_default_max_output_tokens"), 0),
        )

        try:
            decision = await self._service().reserve(reserve_request)
        except QuotaExceededError as exc:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "error": "Code Plan quota exhausted",
                    "balances": exc.decision.balances_before,
                },
            ) from exc
        except QuotaUnavailableError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"error": "Code Plan quota unavailable"},
            ) from exc

        reservation = {
            "request_id": request_id,
            "subscription_id": reserve_request.subscription_id,
            "project_id": reserve_request.project_id,
            "reserved_credits": decision.credits,
            "input_usage": input_usage.model_dump(),
        }
        request_metadata[CODE_PLAN_REQUEST_ID_METADATA_KEY] = request_id
        request_metadata[CODE_PLAN_QUOTA_RESERVATION_METADATA_KEY] = reservation
        self._set_key_metadata_value(user_api_key_dict, CODE_PLAN_QUOTA_RESERVATION_METADATA_KEY, reservation)
        return data

    async def async_post_call_success_hook(
        self,
        data: dict,
        user_api_key_dict: UserAPIKeyAuth,
        response: Any,
    ) -> Any:
        metadata = self._key_metadata(user_api_key_dict)
        reservation = self._reservation_metadata(data, metadata)
        if reservation is None:
            return response

        settlement = self._settlement_request(
            metadata=metadata,
            reservation=reservation,
            actual_usage=self._usage_from_response(response),
            event_type=QuotaEventType.SETTLE,
        )
        await self._service().settle(settlement)
        return response

    async def async_post_call_failure_hook(
        self,
        request_data: dict,
        original_exception: Exception,
        user_api_key_dict: UserAPIKeyAuth,
        traceback_str: str | None = None,
    ) -> HTTPException | None:
        metadata = self._key_metadata(user_api_key_dict)
        reservation = self._reservation_metadata(request_data, metadata)
        if reservation is None:
            return None

        input_usage = CreditUsage.model_validate(reservation["input_usage"])
        status_code = self._exception_status_code(original_exception)
        actual_usage = settlement_usage_for_failure(
            input_usage,
            status_code=status_code,
            gateway_rejected=status_code in {401, 403, 429},
        )
        settlement = self._settlement_request(
            metadata=metadata,
            reservation=reservation,
            actual_usage=actual_usage,
            event_type=QuotaEventType.RELEASE if actual_usage == CreditUsage() else QuotaEventType.SETTLE,
        )
        await self._service().settle(settlement)
        return None

    def _service(self) -> QuotaService:
        if self._quota_service is not None:
            return self._quota_service

        redis_url = os.getenv("CODE_PLAN_QUOTA_REDIS_URL") or os.getenv("REDIS_URL")
        if not redis_url:
            raise QuotaUnavailableError("CODE_PLAN_QUOTA_REDIS_URL is not configured")

        import redis.asyncio as redis

        redis_client = redis.from_url(redis_url, decode_responses=True)
        self._quota_service = QuotaService(RedisQuotaStore(redis_client))
        return self._quota_service

    def _settlement_request(
        self,
        *,
        metadata: dict[str, Any],
        reservation: dict[str, Any],
        actual_usage: CreditUsage,
        event_type: QuotaEventType,
    ) -> QuotaSettlementRequest:
        return QuotaSettlementRequest(
            request_id=str(reservation["request_id"]),
            subscription_id=str(reservation["subscription_id"]),
            project_id=self._optional_str(reservation.get("project_id")),
            actual_usage=actual_usage,
            multipliers=self._multipliers(metadata),
            windows=quota_windows_from_metadata(metadata),
            reserved_credits=float(reservation["reserved_credits"]),
            event_type=event_type,
        )

    def _is_code_plan_key(self, metadata: dict[str, Any]) -> bool:
        return (
            "code_plan_subscription_id" in metadata
            and "code_plan_quota_5h" in metadata
            and "code_plan_quota_weekly" in metadata
        )

    def _key_metadata(self, user_api_key_dict: UserAPIKeyAuth) -> dict[str, Any]:
        metadata = getattr(user_api_key_dict, "metadata", None)
        return metadata if isinstance(metadata, dict) else {}

    def _set_key_metadata_value(self, user_api_key_dict: UserAPIKeyAuth, key: str, value: Any) -> None:
        metadata = getattr(user_api_key_dict, "metadata", None)
        if isinstance(metadata, dict):
            metadata[key] = value

    def _reservation_metadata(self, data: dict, metadata: dict[str, Any]) -> dict[str, Any] | None:
        request_metadata = data.get("metadata")
        if isinstance(request_metadata, dict):
            reservation = request_metadata.get(CODE_PLAN_QUOTA_RESERVATION_METADATA_KEY)
            if isinstance(reservation, dict):
                return reservation
        reservation = metadata.get(CODE_PLAN_QUOTA_RESERVATION_METADATA_KEY)
        return reservation if isinstance(reservation, dict) else None

    def _request_id(self, metadata: dict[str, Any]) -> str:
        request_id = (
            metadata.get(CODE_PLAN_REQUEST_ID_METADATA_KEY)
            or metadata.get("request_id")
            or metadata.get("litellm_call_id")
            or str(uuid4())
        )
        return str(request_id)

    def _multipliers(self, metadata: dict[str, Any]) -> CreditMultipliers:
        return CreditMultipliers(
            input_multiplier=self._float(metadata.get("code_plan_credit_input_multiplier"), 1.0),
            output_multiplier=self._float(metadata.get("code_plan_credit_output_multiplier"), 1.0),
            cache_read_multiplier=self._float(metadata.get("code_plan_credit_cache_read_multiplier"), 0.0),
            cache_write_multiplier=self._float(metadata.get("code_plan_credit_cache_write_multiplier"), 0.0),
        )

    def _input_usage_from_data(self, data: dict) -> CreditUsage:
        return CreditUsage(input_tokens=self._estimate_input_tokens(data))

    def _estimate_input_tokens(self, data: dict) -> int:
        model = data.get("model")
        try:
            if data.get("messages") is not None:
                return int(litellm.token_counter(model=model, messages=data.get("messages")))
            if data.get("prompt") is not None:
                return int(litellm.token_counter(model=model, text=str(data.get("prompt"))))
            if data.get("input") is not None:
                return int(litellm.token_counter(model=model, text=str(data.get("input"))))
        except Exception:
            return 0
        return 0

    def _usage_from_response(self, response: Any) -> CreditUsage:
        usage = self._usage_object(response)
        return CreditUsage(
            input_tokens=self._usage_int(usage, "prompt_tokens", "input_tokens"),
            output_tokens=self._usage_int(usage, "completion_tokens", "output_tokens"),
            cache_read_tokens=self._usage_int(
                usage,
                "cache_read_input_tokens",
                "cache_read_tokens",
                "prompt_cache_hit_tokens",
            ),
            cache_write_tokens=self._usage_int(
                usage,
                "cache_creation_input_tokens",
                "cache_write_tokens",
                "prompt_cache_miss_tokens",
            ),
        )

    def _usage_object(self, response: Any) -> Any:
        if isinstance(response, dict):
            return response.get("usage") or {}
        return getattr(response, "usage", {}) or {}

    def _usage_int(self, usage: Any, *names: str) -> int:
        for name in names:
            value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
            if value is not None:
                return self._int(value, 0)
        return 0

    def _exception_status_code(self, exception: Exception) -> int | None:
        for field_name in ("status_code", "http_status", "code"):
            value = getattr(exception, field_name, None)
            if value is not None:
                return self._int(value, 0)
        return None

    def _optional_str(self, value: Any) -> str | None:
        return None if value is None else str(value)

    def _int(self, value: Any, default: int) -> int:
        try:
            if value is None:
                return default
            return int(value)
        except (TypeError, ValueError):
            return default

    def _float(self, value: Any, default: float) -> float:
        try:
            if value is None:
                return default
            return float(value)
        except (TypeError, ValueError):
            return default
