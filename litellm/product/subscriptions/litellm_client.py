from __future__ import annotations

from typing import Any, Protocol

from litellm.product.subscriptions.models import LiteLLMKeyProvision, SubscriptionRecord
from litellm.proxy._types import (
    GenerateKeyRequest,
    KeyRequest,
    LiteLLMKeyType,
    LitellmUserRoles,
    NewTeamRequest,
    UpdateTeamRequest,
    UserAPIKeyAuth,
)

CODE_PLAN_GATEWAY_ALLOWED_ROUTES = [
    "/v1/chat/completions",
    "/v1/messages",
    "/v1/models",
]


class SubscriptionLiteLLMClient(Protocol):
    async def create_team(self, config: dict[str, Any], actor: UserAPIKeyAuth | None = None) -> str: ...

    async def update_team(
        self,
        team_id: str,
        config: dict[str, Any],
        actor: UserAPIKeyAuth | None = None,
    ) -> None: ...

    async def generate_key(
        self,
        team_id: str,
        subscription: SubscriptionRecord,
        key_alias: str | None = None,
        metadata: dict[str, Any] | None = None,
        actor: UserAPIKeyAuth | None = None,
    ) -> LiteLLMKeyProvision: ...

    async def revoke_key(self, key_id: str, actor: UserAPIKeyAuth | None = None) -> None: ...


class ProxySubscriptionLiteLLMClient:
    def _request(self) -> Any:
        from starlette.requests import Request

        return Request(scope={"type": "http", "headers": []})

    def _actor_or_admin(self, actor: UserAPIKeyAuth | None) -> UserAPIKeyAuth:
        if actor is not None:
            return actor
        return UserAPIKeyAuth(
            api_key="code-plan-subscription",
            user_role=LitellmUserRoles.PROXY_ADMIN,
            user_id="code-plan-subscription",
        )

    def _changed_by(self, actor: UserAPIKeyAuth | None) -> str:
        user_id = getattr(actor, "user_id", None) if actor is not None else None
        return user_id or "code-plan-subscription"

    async def create_team(self, config: dict[str, Any], actor: UserAPIKeyAuth | None = None) -> str:
        from litellm.proxy.management_endpoints.team_endpoints import new_team

        response = await new_team(
            data=NewTeamRequest(**config),
            http_request=self._request(),
            user_api_key_dict=self._actor_or_admin(actor),
            litellm_changed_by=self._changed_by(actor),
        )
        return self._extract_field(response, "team_id")

    async def update_team(self, team_id: str, config: dict[str, Any], actor: UserAPIKeyAuth | None = None) -> None:
        from litellm.proxy.management_endpoints.team_endpoints import update_team

        await update_team(
            data=UpdateTeamRequest(team_id=team_id, **config),
            http_request=self._request(),
            user_api_key_dict=self._actor_or_admin(actor),
            litellm_changed_by=self._changed_by(actor),
        )

    async def generate_key(
        self,
        team_id: str,
        subscription: SubscriptionRecord,
        key_alias: str | None = None,
        metadata: dict[str, Any] | None = None,
        actor: UserAPIKeyAuth | None = None,
    ) -> LiteLLMKeyProvision:
        from litellm.proxy.management_endpoints.key_management_endpoints import generate_key_fn

        key_metadata = {
            "code_plan_subscription_id": subscription.subscription_id,
            "code_plan_project_id": subscription.project_id,
            "code_plan_id": subscription.plan_id,
            "code_plan_version": subscription.plan_snapshot.plan_version,
            "code_plan_quota_5h": subscription.plan_snapshot.quota_5h,
            "code_plan_quota_weekly": subscription.plan_snapshot.quota_weekly,
            "code_plan_default_max_output_tokens": subscription.plan_snapshot.default_max_output_tokens,
            "code_plan_credit_rule_id": subscription.plan_snapshot.credit_rule_id,
            "code_plan_credit_rule_version": subscription.plan_snapshot.credit_rule_version,
            "code_plan_credit_input_multiplier": subscription.plan_snapshot.credit_input_multiplier,
            "code_plan_credit_output_multiplier": subscription.plan_snapshot.credit_output_multiplier,
            "code_plan_credit_cache_read_multiplier": subscription.plan_snapshot.credit_cache_read_multiplier,
            "code_plan_credit_cache_write_multiplier": subscription.plan_snapshot.credit_cache_write_multiplier,
            "code_plan_native_budget_disabled": True,
        }
        quota_monthly = subscription.plan_snapshot.metadata.get("quota_monthly")
        if quota_monthly is not None:
            key_metadata["code_plan_quota_monthly"] = quota_monthly
        key_metadata.update(metadata or {})
        request = GenerateKeyRequest(
            team_id=team_id,
            models=list(subscription.plan_snapshot.allowed_models),
            key_alias=key_alias,
            key_type=LiteLLMKeyType.LLM_API,
            rpm_limit=subscription.plan_snapshot.rpm_limit,
            tpm_limit=subscription.plan_snapshot.tpm_limit,
            max_parallel_requests=subscription.plan_snapshot.max_parallel_requests,
            metadata=key_metadata,
            allowed_routes=list(CODE_PLAN_GATEWAY_ALLOWED_ROUTES),
        )
        response = await generate_key_fn(
            data=request,
            user_api_key_dict=self._actor_or_admin(actor),
            litellm_changed_by=self._changed_by(actor),
        )
        token_id = self._extract_optional_field(response, "token_id") or self._extract_optional_field(response, "token")
        key = self._extract_optional_field(response, "key")
        key_id = token_id or key
        if not key_id:
            raise RuntimeError("LiteLLM key generation did not return a key id")
        return LiteLLMKeyProvision(key_id=key_id, key=key, token_id=token_id)

    async def revoke_key(self, key_id: str, actor: UserAPIKeyAuth | None = None) -> None:
        from litellm.proxy.management_endpoints.key_management_endpoints import delete_key_fn

        await delete_key_fn(
            data=KeyRequest(keys=[key_id]),
            user_api_key_dict=self._actor_or_admin(actor),
            litellm_changed_by=self._changed_by(actor),
        )

    def _extract_field(self, response: Any, field: str) -> str:
        value = self._extract_optional_field(response, field)
        if not value:
            raise RuntimeError(f"LiteLLM response missing {field}")
        return value

    def _extract_optional_field(self, response: Any, field: str) -> str | None:
        if isinstance(response, dict):
            value = response.get(field)
        else:
            value = getattr(response, field, None)
        return str(value) if value else None
