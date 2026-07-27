from __future__ import annotations

import json
import os
from uuid import uuid4

from fastapi import HTTPException, status

import litellm
from litellm._logging import verbose_proxy_logger
from litellm.caching.caching import DualCache
from litellm.integrations.custom_logger import CustomLogger
from litellm.product.quotas.models import (
    CreditMultipliers,
    CreditUsage,
    QuotaEventType,
    QuotaReserveRequest,
    QuotaSettlementRequest,
    QuotaWindow,
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
CODE_PLAN_QUOTA_SETTLED_METADATA_KEY = "_code_plan_quota_settled"
CODE_PLAN_REQUEST_ID_METADATA_KEY = "code_plan_request_id"
DEFAULT_CHARS_PER_TOKEN = 4


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
        if "code_plan_subscription_id" not in metadata:
            return None
        if not self._has_required_quota_metadata(metadata):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"error": "Code Plan quota metadata incomplete"},
            )

        request_metadata = data.get("metadata")
        if not isinstance(request_metadata, dict):
            request_metadata = {}
            data["metadata"] = request_metadata
        request_id = self._request_id(request_metadata)
        input_usage = self._input_usage_from_data(data)
        multipliers = self._multipliers(metadata)
        windows = quota_windows_from_metadata(metadata)
        reserve_request = QuotaReserveRequest(
            request_id=request_id,
            subscription_id=str(metadata["code_plan_subscription_id"]),
            project_id=self._optional_str(metadata.get("code_plan_project_id")),
            input_usage=input_usage,
            multipliers=multipliers,
            windows=windows,
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
            "multipliers": multipliers.model_dump(),
            "windows": [window.model_dump() for window in windows],
        }
        self._stash_value_in_metadata_channels(data, CODE_PLAN_REQUEST_ID_METADATA_KEY, request_id)
        self._stash_value_in_metadata_channels(data, CODE_PLAN_QUOTA_RESERVATION_METADATA_KEY, reservation)
        return data

    async def async_post_call_success_hook(
        self,
        data: dict,
        user_api_key_dict: UserAPIKeyAuth,
        response: object,
    ) -> object:
        metadata = self._key_metadata(user_api_key_dict)
        reservation = self._reservation_metadata(data, metadata)
        await self._settle_reserved_usage(
            data=data,
            metadata=metadata,
            reservation=reservation,
            actual_usage=self._usage_from_response(response),
            event_type=QuotaEventType.SETTLE,
            context="post-call success",
        )
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
        await self._settle_reserved_usage(
            data=request_data,
            metadata=metadata,
            reservation=reservation,
            actual_usage=actual_usage,
            event_type=QuotaEventType.RELEASE if actual_usage == CreditUsage() else QuotaEventType.SETTLE,
            context="post-call failure",
        )
        return None

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        metadata = self._metadata_from_logging_kwargs(kwargs)
        reservation = self._reservation_from_logging_kwargs(kwargs)
        await self._settle_reserved_usage(
            data=kwargs,
            metadata=metadata,
            reservation=reservation,
            actual_usage=self._usage_from_response(response_obj),
            event_type=QuotaEventType.SETTLE,
            context="async success logging",
        )

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        metadata = self._metadata_from_logging_kwargs(kwargs)
        reservation = self._reservation_from_logging_kwargs(kwargs)
        if reservation is None:
            return

        input_usage = CreditUsage.model_validate(reservation["input_usage"])
        recovered_usage = self._recovered_failure_usage(kwargs)
        original_exception = kwargs.get("exception") if isinstance(kwargs, dict) else None
        status_code = self._exception_status_code(original_exception)
        actual_usage = (
            recovered_usage
            if recovered_usage is not None and recovered_usage != CreditUsage()
            else settlement_usage_for_failure(
                input_usage,
                status_code=status_code,
                gateway_rejected=status_code in {401, 403, 429},
            )
        )
        await self._settle_reserved_usage(
            data=kwargs,
            metadata=metadata,
            reservation=reservation,
            actual_usage=actual_usage,
            event_type=QuotaEventType.RELEASE if actual_usage == CreditUsage() else QuotaEventType.SETTLE,
            context="async failure logging",
        )

    async def async_release_max_parallel_requests_on_disconnect(
        self,
        user_api_key_dict: UserAPIKeyAuth,
        request_data: dict | None = None,
    ) -> None:
        metadata = self._key_metadata(user_api_key_dict)
        data = request_data if isinstance(request_data, dict) else {}
        reservation = self._reservation_metadata(data, metadata)
        await self._settle_reserved_usage(
            data=data,
            metadata=metadata,
            reservation=reservation,
            actual_usage=CreditUsage(),
            event_type=QuotaEventType.RELEASE,
            context="stream disconnect",
        )

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

    async def _settle_reserved_usage(
        self,
        *,
        data: object,
        metadata: dict[str, object],
        reservation: dict[str, object] | None,
        actual_usage: CreditUsage,
        event_type: QuotaEventType,
        context: str,
    ) -> None:
        if reservation is None or self._is_reservation_settled(data, reservation):
            return

        try:
            settlement = self._settlement_request(
                metadata=metadata,
                reservation=reservation,
                actual_usage=actual_usage,
                event_type=event_type,
            )
            decision = await self._service().settle(settlement)
        except QuotaUnavailableError as exc:
            verbose_proxy_logger.exception(f"Code Plan quota settlement failed during {context}: {exc}")
            return

        if not decision.allowed:
            verbose_proxy_logger.warning(
                "Code Plan quota settlement was not applied during %s: request_id=%s reason=%s",
                context,
                decision.request_id,
                decision.reason,
            )
            return
        self._mark_reservation_settled(data, reservation)

    def _settlement_request(
        self,
        *,
        metadata: dict[str, object],
        reservation: dict[str, object],
        actual_usage: CreditUsage,
        event_type: QuotaEventType,
    ) -> QuotaSettlementRequest:
        return QuotaSettlementRequest(
            request_id=str(reservation["request_id"]),
            subscription_id=str(reservation["subscription_id"]),
            project_id=self._optional_str(reservation.get("project_id")),
            actual_usage=actual_usage,
            multipliers=self._reservation_multipliers(reservation, metadata),
            windows=self._reservation_windows(reservation, metadata),
            reserved_credits=float(reservation["reserved_credits"]),
            event_type=event_type,
        )

    def _has_required_quota_metadata(self, metadata: dict[str, object]) -> bool:
        return (
            "code_plan_subscription_id" in metadata
            and "code_plan_quota_5h" in metadata
            and "code_plan_quota_weekly" in metadata
        )

    def _reservation_multipliers(
        self,
        reservation: dict[str, object],
        metadata: dict[str, object],
    ) -> CreditMultipliers:
        multipliers = reservation.get("multipliers")
        if isinstance(multipliers, dict):
            return CreditMultipliers.model_validate(multipliers)
        return self._multipliers(metadata)

    def _reservation_windows(
        self,
        reservation: dict[str, object],
        metadata: dict[str, object],
    ) -> list[QuotaWindow]:
        windows = reservation.get("windows")
        if isinstance(windows, list) and windows:
            return [QuotaWindow.model_validate(window) for window in windows]
        return quota_windows_from_metadata(metadata)

    def _key_metadata(self, user_api_key_dict: object) -> dict[str, object]:
        metadata = getattr(user_api_key_dict, "metadata", None)
        return metadata if isinstance(metadata, dict) else {}

    @staticmethod
    def _stash_value_in_metadata_channels(data: dict[str, object], key: str, value: object) -> None:
        for channel in ("metadata", "litellm_metadata"):
            existing = data.get(channel)
            if isinstance(existing, dict):
                existing[key] = value
            elif channel == "metadata":
                data[channel] = {key: value}

    def _reservation_metadata(self, data: dict, metadata: dict[str, object]) -> dict[str, object] | None:
        reservation = self._lookup_stashed_value(data, None, CODE_PLAN_QUOTA_RESERVATION_METADATA_KEY)
        return reservation if isinstance(reservation, dict) else None

    def _reservation_from_logging_kwargs(self, kwargs: object) -> dict[str, object] | None:
        standard_logging_metadata = self._standard_logging_metadata(kwargs)
        reservation = self._lookup_stashed_value(
            kwargs,
            standard_logging_metadata,
            CODE_PLAN_QUOTA_RESERVATION_METADATA_KEY,
        )
        return reservation if isinstance(reservation, dict) else None

    def _metadata_from_logging_kwargs(self, kwargs: object) -> dict[str, object]:
        metadata: dict[str, object] = {}
        if isinstance(kwargs, dict):
            for channel in ("metadata", "litellm_metadata"):
                channel_dict = kwargs.get(channel)
                if isinstance(channel_dict, dict):
                    metadata.update(channel_dict)
            litellm_params = kwargs.get("litellm_params")
            if isinstance(litellm_params, dict):
                lp_metadata = litellm_params.get("metadata")
                if isinstance(lp_metadata, dict):
                    metadata.update(lp_metadata)
            user_api_key_dict = kwargs.get("user_api_key_dict")
            metadata.update(self._key_metadata(user_api_key_dict))
        standard_logging_metadata = self._standard_logging_metadata(kwargs)
        if isinstance(standard_logging_metadata, dict):
            metadata.update(standard_logging_metadata)
        return metadata

    @staticmethod
    def _standard_logging_metadata(kwargs: object) -> dict[str, object] | None:
        if not isinstance(kwargs, dict):
            return None
        standard_logging_object = kwargs.get("standard_logging_object")
        if not isinstance(standard_logging_object, dict):
            return None
        metadata = standard_logging_object.get("metadata")
        return metadata if isinstance(metadata, dict) else None

    @staticmethod
    def _lookup_stashed_value(
        kwargs: object,
        standard_logging_metadata: dict[str, object] | None,
        key: str,
    ) -> object:
        candidate: object = None
        if isinstance(kwargs, dict):
            for channel in ("metadata", "litellm_metadata"):
                channel_dict = kwargs.get(channel)
                if isinstance(channel_dict, dict) and key in channel_dict:
                    candidate = channel_dict.get(key)
                    if candidate is not None:
                        return candidate
            litellm_params = kwargs.get("litellm_params")
            if isinstance(litellm_params, dict):
                lp_metadata = litellm_params.get("metadata")
                if isinstance(lp_metadata, dict) and key in lp_metadata:
                    candidate = lp_metadata.get(key)
        if candidate is None and isinstance(standard_logging_metadata, dict):
            candidate = standard_logging_metadata.get(key)
        return candidate

    def _is_reservation_settled(self, data: object, reservation: dict[str, object]) -> bool:
        if bool(reservation.get(CODE_PLAN_QUOTA_SETTLED_METADATA_KEY)):
            return True
        return bool(
            self._lookup_stashed_value(
                data,
                self._standard_logging_metadata(data),
                CODE_PLAN_QUOTA_SETTLED_METADATA_KEY,
            )
        )

    def _mark_reservation_settled(self, data: object, reservation: dict[str, object]) -> None:
        reservation[CODE_PLAN_QUOTA_SETTLED_METADATA_KEY] = True
        if not isinstance(data, dict):
            return

        data[CODE_PLAN_QUOTA_SETTLED_METADATA_KEY] = True
        for channel in ("metadata", "litellm_metadata"):
            channel_dict = data.get(channel)
            if isinstance(channel_dict, dict):
                channel_dict[CODE_PLAN_QUOTA_SETTLED_METADATA_KEY] = True
                self._mark_nested_reservation_settled(channel_dict)

        litellm_params = data.get("litellm_params")
        if isinstance(litellm_params, dict):
            lp_metadata = litellm_params.get("metadata")
            if isinstance(lp_metadata, dict):
                lp_metadata[CODE_PLAN_QUOTA_SETTLED_METADATA_KEY] = True
                self._mark_nested_reservation_settled(lp_metadata)

        standard_logging_metadata = self._standard_logging_metadata(data)
        if isinstance(standard_logging_metadata, dict):
            standard_logging_metadata[CODE_PLAN_QUOTA_SETTLED_METADATA_KEY] = True
            self._mark_nested_reservation_settled(standard_logging_metadata)

    @staticmethod
    def _mark_nested_reservation_settled(metadata: dict[str, object]) -> None:
        reservation = metadata.get(CODE_PLAN_QUOTA_RESERVATION_METADATA_KEY)
        if isinstance(reservation, dict):
            reservation[CODE_PLAN_QUOTA_SETTLED_METADATA_KEY] = True

    def _request_id(self, metadata: dict[str, object]) -> str:
        request_id = (
            metadata.get(CODE_PLAN_REQUEST_ID_METADATA_KEY)
            or metadata.get("request_id")
            or metadata.get("litellm_call_id")
            or str(uuid4())
        )
        return str(request_id)

    def _multipliers(self, metadata: dict[str, object]) -> CreditMultipliers:
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
        except Exception as exc:  # noqa: BLE001  # litellm.token_counter raises provider-specific errors
            estimate = self._fallback_input_token_estimate(data)
            verbose_proxy_logger.debug(
                "Code Plan quota token estimation used fallback estimate=%s error=%s",
                estimate,
                exc,
            )
            return estimate
        return 0

    def _fallback_input_token_estimate(self, data: dict) -> int:
        for field_name in ("messages", "prompt", "input"):
            value = data.get(field_name)
            if value is not None:
                return self._serialized_token_estimate(value)
        return 0

    def _serialized_token_estimate(self, value: object) -> int:
        try:
            serialized = json.dumps(value, default=str, separators=(",", ":"))
        except (TypeError, ValueError):
            serialized = str(value)
        if not serialized:
            return 0
        return max(1, (len(serialized) + DEFAULT_CHARS_PER_TOKEN - 1) // DEFAULT_CHARS_PER_TOKEN)

    def _usage_from_response(self, response: object) -> CreditUsage:
        usage = self._usage_object(response)
        return self._usage_from_usage_object(usage)

    def _usage_from_usage_object(self, usage: object) -> CreditUsage:
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

    def _usage_object(self, response: object) -> object:
        if isinstance(response, dict):
            return response.get("usage") or {}
        return getattr(response, "usage", {}) or {}

    def _recovered_failure_usage(self, kwargs: object) -> CreditUsage | None:
        if not isinstance(kwargs, dict):
            return None
        combined_usage = kwargs.get("combined_usage_object")
        if combined_usage is None:
            return None
        return self._usage_from_usage_object(combined_usage)

    def _usage_int(self, usage: object, *names: str) -> int:
        for name in names:
            value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
            if value is not None:
                return self._int(value, 0)
        return 0

    def _exception_status_code(self, exception: object) -> int | None:
        if exception is None:
            return None
        for field_name in ("status_code", "http_status", "code"):
            value = getattr(exception, field_name, None)
            if value is not None:
                return self._int(value, 0)
        return None

    def _optional_str(self, value: object) -> str | None:
        return None if value is None else str(value)

    def _int(self, value: object, default: int) -> int:
        try:
            if value is None:
                return default
            return int(value)
        except (TypeError, ValueError):
            return default

    def _float(self, value: object, default: float) -> float:
        try:
            if value is None:
                return default
            return float(value)
        except (TypeError, ValueError):
            return default
