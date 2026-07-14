from __future__ import annotations

import asyncio
import fnmatch
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence, cast

from fastapi import HTTPException, status

import litellm
from litellm.caching import DualCache
from litellm.proxy._types import UserAPIKeyAuth
from litellm.proxy.auth.auth_utils import get_model_from_request
from litellm.proxy.auth.route_checks import RouteChecks

CODE_PLAN_RESERVATION_METADATA_KEY = "user_api_key_code_plan_reservation"

_CODE_PLAN_METADATA_KEYS = ("code_plan", "codePlan")
_ACTIVE_STATUS = "active"
_DEFAULT_OUTPUT_TOKEN_RESERVATION = 4096
_DEFAULT_WINDOW_DURATIONS = {
    "five_hour": 5 * 60 * 60,
    "5h": 5 * 60 * 60,
    "weekly": 7 * 24 * 60 * 60,
    "week": 7 * 24 * 60 * 60,
}
_RESERVATION_LOCKS: dict[str, asyncio.Lock] = {}


@dataclass(frozen=True)
class CodePlanWindow:
    name: str
    limit_credits: float
    window_id: str
    ttl_seconds: int


@dataclass(frozen=True)
class CodePlanProjection:
    subscription_id: str
    key_id: str
    subscription_status: str
    key_status: str
    allowed_models: tuple[str, ...] | None
    windows: tuple[CodePlanWindow, ...]
    input_multiplier: float
    output_multiplier: float
    model_multipliers: Mapping[str, Mapping[str, Any]]
    policy_version: str | None
    multiplier_version: str | None
    default_max_output_tokens: int
    upstream_api_base: str | None
    upstream_api_key_env: str | None
    upstream_model_map: Mapping[str, str]
    raw_projection: Mapping[str, Any]

    def multipliers_for_model(self, model: str | None) -> tuple[float, float]:
        if model:
            for pattern, multiplier in self.model_multipliers.items():
                if fnmatch.fnmatch(model, pattern):
                    return (
                        _positive_float(
                            multiplier.get("input") or multiplier.get("input_multiplier"), self.input_multiplier
                        ),
                        _positive_float(
                            multiplier.get("output") or multiplier.get("output_multiplier"), self.output_multiplier
                        ),
                    )
        return self.input_multiplier, self.output_multiplier

    def is_model_allowed(self, model: str | None) -> bool:
        if not self.allowed_models or "*" in self.allowed_models:
            return True
        if model is None:
            return False
        return any(fnmatch.fnmatch(model, pattern) for pattern in self.allowed_models)


class EntitlementSync:
    @staticmethod
    def projection_from_auth(
        valid_token: UserAPIKeyAuth,
        team_object: Any | None = None,
        project_object: Any | None = None,
        now: float | None = None,
    ) -> CodePlanProjection | None:
        raw_projection = _merged_code_plan_projection(
            getattr(valid_token, "team_metadata", None),
            getattr(team_object, "metadata", None),
            getattr(valid_token, "project_metadata", None),
            getattr(project_object, "metadata", None),
            getattr(valid_token, "metadata", None),
        )
        if raw_projection is None:
            return None

        subscription_id = _string_value(
            raw_projection.get("subscription_id")
            or raw_projection.get("subscriptionId")
            or raw_projection.get("subscription")
        )
        if subscription_id is None:
            raise _code_plan_error(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                code="entitlement_projection_invalid",
                message="Code Plan entitlement projection is missing subscription_id.",
            )

        key_id = _string_value(raw_projection.get("key_id") or raw_projection.get("keyId") or valid_token.token)
        if key_id is None:
            raise _code_plan_error(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                code="entitlement_projection_invalid",
                message="Code Plan entitlement projection is missing key_id.",
            )

        windows = _parse_windows(raw_projection, now=now)
        if not windows:
            raise _code_plan_error(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                code="quota_policy_missing",
                message="Code Plan quota policy is missing quota_windows.",
            )

        upstream = raw_projection.get("newapi") or raw_projection.get("upstream") or {}
        upstream_dict = upstream if isinstance(upstream, Mapping) else {}
        return CodePlanProjection(
            subscription_id=subscription_id,
            key_id=key_id,
            subscription_status=str(
                raw_projection.get("subscription_status") or raw_projection.get("status") or _ACTIVE_STATUS
            ).lower(),
            key_status=str(
                raw_projection.get("key_status") or raw_projection.get("api_key_status") or _ACTIVE_STATUS
            ).lower(),
            allowed_models=_tuple_of_strings(raw_projection.get("allowed_models") or raw_projection.get("models")),
            windows=tuple(windows),
            input_multiplier=_positive_float(
                raw_projection.get("input_credit_multiplier") or raw_projection.get("input_multiplier"), 1.0
            ),
            output_multiplier=_positive_float(
                raw_projection.get("output_credit_multiplier") or raw_projection.get("output_multiplier"), 1.0
            ),
            model_multipliers=_mapping_value(raw_projection.get("model_multipliers")),
            policy_version=_string_value(
                raw_projection.get("quota_policy_version") or raw_projection.get("policy_version")
            ),
            multiplier_version=_string_value(raw_projection.get("multiplier_version")),
            default_max_output_tokens=_positive_int(
                raw_projection.get("default_max_output_tokens"), _DEFAULT_OUTPUT_TOKEN_RESERVATION
            ),
            upstream_api_base=_string_value(upstream_dict.get("api_base") or upstream_dict.get("base_url")),
            upstream_api_key_env=_string_value(
                upstream_dict.get("api_key_env") or upstream_dict.get("service_key_env")
            ),
            upstream_model_map=_string_mapping(upstream_dict.get("model_map") or upstream_dict.get("models")),
            raw_projection=raw_projection,
        )


class CodePlanAuthService:
    @staticmethod
    def validate_request(
        valid_token: UserAPIKeyAuth,
        request_body: dict[str, Any],
        route: str,
        team_object: Any | None = None,
        project_object: Any | None = None,
    ) -> CodePlanProjection | None:
        if not RouteChecks.is_llm_api_route(route=route):
            return None
        projection = EntitlementSync.projection_from_auth(
            valid_token=valid_token,
            team_object=team_object,
            project_object=project_object,
        )
        if projection is None:
            return None

        if projection.key_status != _ACTIVE_STATUS:
            raise _code_plan_error(
                status_code=status.HTTP_401_UNAUTHORIZED,
                code="invalid_api_key",
                message="Code Plan key is not active.",
            )
        if projection.subscription_status != _ACTIVE_STATUS:
            raise _code_plan_error(
                status_code=status.HTTP_403_FORBIDDEN,
                code="subscription_inactive",
                message="Code Plan subscription is not active.",
            )

        model = _request_model(request_body)
        if not projection.is_model_allowed(model):
            raise _code_plan_error(
                status_code=status.HTTP_403_FORBIDDEN,
                code="model_not_allowed",
                message="Model is not allowed by this Code Plan subscription.",
            )
        return projection


class CodePlanQuotaManager:
    @staticmethod
    async def reserve_for_request(
        request_body: dict[str, Any],
        projection: CodePlanProjection,
        user_api_key_cache: DualCache,
    ) -> dict[str, Any]:
        request_id = _ensure_request_id(request_body)
        model = _request_model(request_body)
        prompt_tokens = _estimate_prompt_tokens(request_body=request_body, model=model)
        max_output_tokens = _max_output_tokens(request_body=request_body, projection=projection)
        input_multiplier, output_multiplier = projection.multipliers_for_model(model)
        input_credits = float(prompt_tokens) * input_multiplier
        reserved_credits = max(1.0, input_credits + float(max_output_tokens) * output_multiplier)
        entries: list[dict[str, Any]] = []
        lock = _reservation_lock(projection.subscription_id)

        async with lock:
            try:
                for window in projection.windows:
                    counter_key = _counter_key(projection=projection, window=window)
                    current_credits = await user_api_key_cache.async_increment_cache(
                        key=counter_key,
                        value=reserved_credits,
                        ttl=window.ttl_seconds,
                        refresh_ttl=False,
                    )
                    if current_credits is None:
                        raise _code_plan_error(
                            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            code="quota_service_unavailable",
                            message="Code Plan quota cache is unavailable.",
                        )
                    entry = {
                        "window": window.name,
                        "window_id": window.window_id,
                        "counter_key": counter_key,
                        "limit_credits": window.limit_credits,
                        "reserved_credits": reserved_credits,
                        "applied_adjustment": 0.0,
                        "ttl_seconds": window.ttl_seconds,
                    }
                    entries.append(entry)
                    if float(current_credits) > window.limit_credits:
                        raise _code_plan_error(
                            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                            code="quota_exceeded",
                            message="Code Plan quota is exhausted.",
                        )
            except (HTTPException, RuntimeError, TypeError, ValueError):
                await _release_entries(user_api_key_cache=user_api_key_cache, entries=entries)
                raise

        return {
            "type": "code_plan",
            "reservation_id": str(uuid.uuid4()),
            "request_id": request_id,
            "subscription_id": projection.subscription_id,
            "key_id": projection.key_id,
            "model": model,
            "reserved_credits": reserved_credits,
            "input_credits": input_credits,
            "input_tokens_reserved": prompt_tokens,
            "output_tokens_reserved": max_output_tokens,
            "input_multiplier": input_multiplier,
            "output_multiplier": output_multiplier,
            "policy_version": projection.policy_version,
            "multiplier_version": projection.multiplier_version,
            "entries": entries,
            "finalized": False,
        }


class UsageRecorder:
    @staticmethod
    async def settle_from_callback(
        reservation: dict[str, Any] | None,
        standard_logging_object: Mapping[str, Any] | None,
        completion_response: Any | None,
    ) -> None:
        if reservation is None:
            return
        prompt_tokens, completion_tokens = _usage_tokens(
            standard_logging_object=standard_logging_object,
            completion_response=completion_response,
        )
        if prompt_tokens is None and completion_tokens is None:
            await reconcile_code_plan_reservation(
                code_plan_reservation=reservation,
                actual_credits=reservation.get("reserved_credits"),
            )
            return

        actual_credits = _credits_for_usage(
            code_plan_reservation=reservation,
            prompt_tokens=prompt_tokens or 0,
            completion_tokens=completion_tokens or 0,
        )
        await reconcile_code_plan_reservation(
            code_plan_reservation=reservation,
            actual_credits=actual_credits,
        )


class CodePlanUpstreamService:
    @staticmethod
    def apply_upstream_request(data: dict[str, Any], valid_token: UserAPIKeyAuth) -> dict[str, Any]:
        projection = EntitlementSync.projection_from_auth(valid_token=valid_token)
        if projection is None or projection.upstream_api_base is None:
            return data
        if projection.upstream_api_key_env is None:
            raise _code_plan_error(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                code="upstream_unavailable",
                message="Code Plan NewAPI upstream credential is not configured.",
            )
        api_key = os.getenv(projection.upstream_api_key_env)
        if not api_key:
            raise _code_plan_error(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                code="upstream_unavailable",
                message="Code Plan NewAPI upstream credential is not available.",
            )

        request_model = data.get("model") if isinstance(data.get("model"), str) else None
        mapped_model = projection.upstream_model_map.get(cast(str, request_model)) if request_model else None
        if mapped_model:
            data["model"] = mapped_model
        data["api_base"] = projection.upstream_api_base
        data["api_key"] = api_key
        extra_headers = dict(data.get("extra_headers") or {})
        extra_headers.setdefault("x-code-plan-request-id", _ensure_request_id(data))
        extra_headers.setdefault("x-code-plan-subscription-id", projection.subscription_id)
        data["extra_headers"] = extra_headers
        return data


async def release_code_plan_reservation(
    code_plan_reservation: dict[str, Any] | None,
) -> None:
    await reconcile_code_plan_reservation(code_plan_reservation=code_plan_reservation, actual_credits=0.0)


async def release_code_plan_reservation_on_cancel(
    code_plan_reservation: dict[str, Any] | None,
) -> None:
    if not code_plan_reservation or code_plan_reservation.get("finalized") is True:
        return
    try:
        await asyncio.shield(
            reconcile_code_plan_reservation(
                code_plan_reservation=code_plan_reservation,
                actual_credits=code_plan_reservation.get("input_credits") or 0.0,
            )
        )
    except (asyncio.CancelledError, RuntimeError, TypeError, ValueError):
        return


async def reconcile_code_plan_reservation(
    code_plan_reservation: dict[str, Any] | None,
    actual_credits: float | None,
) -> None:
    if not code_plan_reservation or code_plan_reservation.get("finalized") is True:
        return
    user_api_key_cache = _get_user_api_key_cache()
    reserved_credits = float(code_plan_reservation.get("reserved_credits") or 0.0)
    actual = float(actual_credits or 0.0)
    for entry in code_plan_reservation.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        await _set_entry_actual_credits(
            user_api_key_cache=user_api_key_cache,
            entry=entry,
            reserved_credits=reserved_credits,
            actual_credits=actual,
        )
    code_plan_reservation["actual_credits"] = actual
    code_plan_reservation["finalized"] = True


def code_plan_reservation_from_metadata(metadata: Mapping[str, Any]) -> dict[str, Any] | None:
    reservation = metadata.get(CODE_PLAN_RESERVATION_METADATA_KEY)
    if isinstance(reservation, dict):
        return reservation
    user_api_key_auth_obj = metadata.get("user_api_key_auth")
    if user_api_key_auth_obj is None:
        return None
    if isinstance(user_api_key_auth_obj, dict):
        reservation = user_api_key_auth_obj.get("code_plan_reservation")
        return reservation if isinstance(reservation, dict) else None
    return getattr(user_api_key_auth_obj, "code_plan_reservation", None)


async def _set_entry_actual_credits(
    user_api_key_cache: DualCache,
    entry: dict[str, Any],
    reserved_credits: float,
    actual_credits: float,
) -> None:
    counter_key = entry.get("counter_key")
    if not counter_key:
        return
    target_adjustment = actual_credits - float(entry.get("reserved_credits") or reserved_credits)
    applied_adjustment = float(entry.get("applied_adjustment") or 0.0)
    adjustment = target_adjustment - applied_adjustment
    if adjustment == 0:
        return
    await user_api_key_cache.async_increment_cache(
        key=counter_key,
        value=adjustment,
        ttl=entry.get("ttl_seconds"),
        refresh_ttl=False,
    )
    entry["applied_adjustment"] = applied_adjustment + adjustment


async def _release_entries(user_api_key_cache: DualCache, entries: Sequence[Mapping[str, Any]]) -> None:
    for entry in entries:
        counter_key = entry.get("counter_key")
        if counter_key:
            await user_api_key_cache.async_increment_cache(
                key=counter_key,
                value=-float(entry.get("reserved_credits") or 0.0),
                ttl=entry.get("ttl_seconds"),
                refresh_ttl=False,
            )


def _merged_code_plan_projection(*metadata_sources: Any | None) -> dict[str, Any] | None:
    merged: dict[str, Any] = {}
    for metadata in metadata_sources:
        projection = _extract_code_plan_projection(metadata)
        if projection:
            merged = _deep_merge(merged, projection)
    return merged or None


def _extract_code_plan_projection(metadata: Any | None) -> Mapping[str, Any] | None:
    if not isinstance(metadata, Mapping):
        return None
    for key in _CODE_PLAN_METADATA_KEYS:
        projection = metadata.get(key)
        if isinstance(projection, Mapping):
            return projection
    return None


def _deep_merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = _deep_merge(dict(current), value)
        else:
            merged[key] = value
    return merged


def _parse_windows(raw_projection: Mapping[str, Any], now: float | None = None) -> list[CodePlanWindow]:
    raw_windows = raw_projection.get("quota_windows") or raw_projection.get("windows")
    windows: list[CodePlanWindow] = []
    if isinstance(raw_windows, Mapping):
        for name, raw_window in raw_windows.items():
            parsed = _parse_window(str(name), raw_window, now=now)
            if parsed is not None:
                windows.append(parsed)
    elif isinstance(raw_windows, Sequence) and not isinstance(raw_windows, (str, bytes)):
        for raw_window in raw_windows:
            if isinstance(raw_window, Mapping):
                name = _string_value(raw_window.get("name") or raw_window.get("type")) or "window"
                parsed = _parse_window(name, raw_window, now=now)
                if parsed is not None:
                    windows.append(parsed)
    return windows


def _parse_window(name: str, raw_window: Any, now: float | None = None) -> CodePlanWindow | None:
    if not isinstance(raw_window, Mapping):
        return None
    limit_credits = _optional_positive_float(
        raw_window.get("limit_credits")
        or raw_window.get("limit")
        or raw_window.get("max_credits")
        or raw_window.get("quota_credits")
        or raw_window.get("credits")
    )
    if limit_credits is None:
        return None

    now_value = now or time.time()
    duration_seconds = _duration_seconds(name=name, raw_window=raw_window)
    window_id = _string_value(raw_window.get("window_id") or raw_window.get("id"))
    ttl_seconds = _positive_int(raw_window.get("ttl_seconds") or raw_window.get("ttl"), duration_seconds)
    reset_at = _reset_timestamp(raw_window.get("reset_at") or raw_window.get("resetAt"))
    if reset_at is not None:
        ttl_seconds = max(1, int(reset_at - now_value))
        window_id = window_id or str(int(reset_at))
    if window_id is None:
        window_id = str(int(now_value // duration_seconds))
        ttl_seconds = max(1, int(duration_seconds - (now_value % duration_seconds)))
    return CodePlanWindow(
        name=name,
        limit_credits=limit_credits,
        window_id=window_id,
        ttl_seconds=ttl_seconds,
    )


def _duration_seconds(name: str, raw_window: Mapping[str, Any]) -> int:
    raw_duration = raw_window.get("duration_seconds") or raw_window.get("duration")
    if isinstance(raw_duration, (int, float)) and raw_duration > 0:
        return int(raw_duration)
    if isinstance(raw_duration, str):
        parsed = _parse_duration_string(raw_duration)
        if parsed is not None:
            return parsed
    return _DEFAULT_WINDOW_DURATIONS.get(name, _DEFAULT_WINDOW_DURATIONS.get(name.lower(), 24 * 60 * 60))


def _parse_duration_string(raw_duration: str) -> int | None:
    stripped = raw_duration.strip().lower()
    if not stripped:
        return None
    suffix = stripped[-1]
    try:
        value = float(stripped[:-1]) if suffix.isalpha() else float(stripped)
    except ValueError:
        return None
    multipliers = {"s": 1, "m": 60, "h": 60 * 60, "d": 24 * 60 * 60, "w": 7 * 24 * 60 * 60}
    return int(value * multipliers.get(suffix, 1))


def _request_model(request_body: Mapping[str, Any]) -> str | None:
    try:
        model = get_model_from_request(request_body=dict(request_body))
    except (KeyError, TypeError, ValueError):
        model = request_body.get("model")
    return model if isinstance(model, str) else None


def _ensure_request_id(request_body: dict[str, Any]) -> str:
    request_id = request_body.get("litellm_call_id") or request_body.get("request_id")
    if isinstance(request_id, str) and request_id:
        request_body["litellm_call_id"] = request_id
        return request_id
    request_id = str(uuid.uuid4())
    request_body["litellm_call_id"] = request_id
    return request_id


def _estimate_prompt_tokens(request_body: Mapping[str, Any], model: str | None) -> int:
    explicit_prompt_tokens = _optional_positive_int(request_body.get("prompt_tokens"))
    if explicit_prompt_tokens is not None:
        return explicit_prompt_tokens
    try:
        messages = request_body.get("messages")
        if messages is not None:
            return int(litellm.token_counter(model=model, messages=messages))
        prompt = request_body.get("prompt") or request_body.get("input")
        if prompt is not None:
            return int(litellm.token_counter(model=model, text=str(prompt)))
    except (KeyError, RuntimeError, TypeError, ValueError):
        pass
    return _rough_token_count(request_body.get("messages") or request_body.get("prompt") or request_body.get("input"))


def _rough_token_count(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, str):
        return max(1, len(value) // 4)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return sum(_rough_token_count(item) for item in value)
    if isinstance(value, Mapping):
        return sum(_rough_token_count(item) for item in value.values())
    return max(1, len(str(value)) // 4)


def _max_output_tokens(request_body: Mapping[str, Any], projection: CodePlanProjection) -> int:
    return _positive_int(
        request_body.get("max_completion_tokens") or request_body.get("max_tokens"),
        projection.default_max_output_tokens,
    )


def _usage_tokens(
    standard_logging_object: Mapping[str, Any] | None,
    completion_response: Any | None,
) -> tuple[int | None, int | None]:
    if standard_logging_object is not None:
        prompt_tokens = _optional_positive_int(standard_logging_object.get("prompt_tokens"))
        completion_tokens = _optional_positive_int(standard_logging_object.get("completion_tokens"))
        if prompt_tokens is not None or completion_tokens is not None:
            return prompt_tokens, completion_tokens

    usage = getattr(completion_response, "usage", None)
    if usage is None and isinstance(completion_response, Mapping):
        usage = completion_response.get("usage")
    if usage is None:
        return None, None
    if isinstance(usage, Mapping):
        return (
            _optional_positive_int(usage.get("prompt_tokens") or usage.get("input_tokens")),
            _optional_positive_int(usage.get("completion_tokens") or usage.get("output_tokens")),
        )
    return (
        _optional_positive_int(getattr(usage, "prompt_tokens", None) or getattr(usage, "input_tokens", None)),
        _optional_positive_int(getattr(usage, "completion_tokens", None) or getattr(usage, "output_tokens", None)),
    )


def _credits_for_usage(code_plan_reservation: Mapping[str, Any], prompt_tokens: int, completion_tokens: int) -> float:
    input_multiplier = float(code_plan_reservation.get("input_multiplier") or 1.0)
    output_multiplier = float(code_plan_reservation.get("output_multiplier") or 1.0)
    return float(prompt_tokens) * input_multiplier + float(completion_tokens) * output_multiplier


def _counter_key(projection: CodePlanProjection, window: CodePlanWindow) -> str:
    return f"code_plan:{projection.subscription_id}:{window.name}:{window.window_id}:credits"


def _reservation_lock(subscription_id: str) -> asyncio.Lock:
    lock = _RESERVATION_LOCKS.get(subscription_id)
    if lock is None:
        lock = asyncio.Lock()
        _RESERVATION_LOCKS[subscription_id] = lock
    return lock


def _get_user_api_key_cache() -> DualCache:
    from litellm.proxy.proxy_server import user_api_key_cache

    return user_api_key_cache


def _code_plan_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={
            "error": {
                "message": message,
                "type": "code_plan_error",
                "code": code,
            }
        },
    )


def _reset_timestamp(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
        except ValueError:
            return None
    return None


def _tuple_of_strings(value: Any) -> tuple[str, ...] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        values = tuple(str(item) for item in value if item is not None)
        return values or None
    return None


def _string_mapping(value: Any) -> Mapping[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): str(item) for key, item in value.items() if item is not None}


def _mapping_value(value: Any) -> Mapping[str, Mapping[str, Any]]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items() if isinstance(item, Mapping)}


def _string_value(value: Any) -> str | None:
    if value is None:
        return None
    result = str(value)
    return result if result else None


def _positive_int(value: Any, default: int) -> int:
    parsed = _optional_positive_int(value)
    return parsed if parsed is not None else default


def _optional_positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _positive_float(value: Any, default: float) -> float:
    parsed = _optional_positive_float(value)
    return parsed if parsed is not None else default


def _optional_positive_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None
