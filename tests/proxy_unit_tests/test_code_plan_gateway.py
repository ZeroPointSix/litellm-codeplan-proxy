import pytest
from fastapi import HTTPException

from litellm.caching.caching import DualCache
from litellm.proxy._types import UserAPIKeyAuth
import litellm.proxy.code_plan as code_plan
from litellm.proxy.code_plan import (
    CodePlanAuthService,
    CodePlanQuotaManager,
    CodePlanUpstreamService,
    EntitlementSync,
    UsageRecorder,
)


def _projection(**overrides):
    base = {
        "subscription_id": "sub_shared",
        "key_id": "key_1",
        "subscription_status": "active",
        "key_status": "active",
        "allowed_models": ["code-plan-*"],
        "quota_windows": {
            "five_hour": {"limit_credits": 30, "duration_seconds": 300, "window_id": "fh"},
            "weekly": {"limit_credits": 30, "duration_seconds": 600, "window_id": "wk"},
        },
        "input_multiplier": 1,
        "output_multiplier": 1,
        "default_max_output_tokens": 10,
        "newapi": {
            "api_base": "http://newapi.internal/v1",
            "api_key_env": "CODE_PLAN_NEWAPI_KEY",
            "model_map": {"code-plan-pro": "openai/gpt-4.1"},
        },
    }
    base.update(overrides)
    return base


def _token(projection=None):
    return UserAPIKeyAuth(
        token="hashed-key",
        metadata={"code_plan": projection or _projection()},
    )


def _request(max_tokens=10):
    return {
        "model": "code-plan-pro",
        "messages": [{"role": "user", "content": "hello"}],
        "prompt_tokens": 1,
        "max_tokens": max_tokens,
    }


def test_code_plan_auth_rejects_inactive_subscription():
    token = _token(_projection(subscription_status="past_due"))

    with pytest.raises(HTTPException) as exc_info:
        CodePlanAuthService.validate_request(
            valid_token=token,
            request_body=_request(),
            route="/chat/completions",
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["error"]["code"] == "subscription_inactive"


@pytest.mark.asyncio
async def test_code_plan_quota_is_shared_by_subscription_and_rolls_back_failed_window():
    token = _token(
        _projection(
            quota_windows={
                "five_hour": {"limit_credits": 30, "duration_seconds": 300, "window_id": "fh"},
                "weekly": {"limit_credits": 15, "duration_seconds": 600, "window_id": "wk"},
            }
        )
    )
    projection = EntitlementSync.projection_from_auth(valid_token=token)
    assert projection is not None
    cache = DualCache()

    first = await CodePlanQuotaManager.reserve_for_request(
        request_body=_request(),
        projection=projection,
        user_api_key_cache=cache,
    )
    assert first["reserved_credits"] == 11

    with pytest.raises(HTTPException) as exc_info:
        await CodePlanQuotaManager.reserve_for_request(
            request_body=_request(),
            projection=projection,
            user_api_key_cache=cache,
        )

    assert exc_info.value.status_code == 429
    for entry in first["entries"]:
        assert await cache.async_get_cache(entry["counter_key"]) == 11


@pytest.mark.asyncio
async def test_code_plan_usage_settlement_refunds_unused_reserved_credits(monkeypatch):
    token = _token()
    projection = EntitlementSync.projection_from_auth(valid_token=token)
    assert projection is not None
    cache = DualCache()
    monkeypatch.setattr(code_plan, "_get_user_api_key_cache", lambda: cache)

    reservation = await CodePlanQuotaManager.reserve_for_request(
        request_body=_request(),
        projection=projection,
        user_api_key_cache=cache,
    )

    await UsageRecorder.settle_from_callback(
        reservation=reservation,
        standard_logging_object={"prompt_tokens": 2, "completion_tokens": 3},
        completion_response=None,
    )

    assert reservation["actual_credits"] == 5
    assert reservation["finalized"] is True
    for entry in reservation["entries"]:
        assert await cache.async_get_cache(entry["counter_key"]) == 5


def test_code_plan_upstream_uses_internal_newapi_credential(monkeypatch):
    monkeypatch.setenv("CODE_PLAN_NEWAPI_KEY", "internal-service-key")
    data = {"model": "code-plan-pro", "litellm_call_id": "req-123", "api_key": "sk-user-key"}

    updated = CodePlanUpstreamService.apply_upstream_request(data=data, valid_token=_token())

    assert updated["api_base"] == "http://newapi.internal/v1"
    assert updated["api_key"] == "internal-service-key"
    assert updated["model"] == "openai/gpt-4.1"
    assert updated["extra_headers"]["x-code-plan-request-id"] == "req-123"
    assert updated["extra_headers"]["x-code-plan-subscription-id"] == "sub_shared"
