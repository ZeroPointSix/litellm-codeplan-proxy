from __future__ import annotations

from datetime import datetime, timezone
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

_CODE_PLAN_METADATA_PREFIX = "code_plan_"


def merge_code_plan_key_metadata(
    system_metadata: dict[str, Any],
    user_metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    """Merge caller metadata without letting it override Code Plan system fields."""
    sanitized_user = {
        key: value
        for key, value in (user_metadata or {}).items()
        if not str(key).startswith(_CODE_PLAN_METADATA_PREFIX)
    }
    merged = dict(sanitized_user)
    merged.update(system_metadata)
    return merged


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

        system_metadata = {
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
            "code_plan_week_anchor_epoch": self._week_anchor_epoch(subscription),
            "code_plan_subscription_started_at": self._iso_or_none(subscription.created_at),
            "code_plan_renewed_at": self._iso_or_none(subscription.renewed_at),
        }
        system_metadata.update(self._preserved_5h_anchor_metadata(subscription))
        quota_monthly = subscription.plan_snapshot.metadata.get("quota_monthly")
        if quota_monthly is not None:
            system_metadata["code_plan_quota_monthly"] = quota_monthly
        key_metadata = merge_code_plan_key_metadata(system_metadata, metadata)
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

    def _preserved_5h_anchor_metadata(self, subscription: SubscriptionRecord) -> dict[str, Any]:
        """Preserve only system-recorded anchors; ignore caller-supplied key metadata."""
        window_anchor = self._epoch_or_none(subscription.window_5h_start)
        if window_anchor is not None:
            return {
                "code_plan_5h_anchor_epoch": window_anchor,
                "code_plan_5h_period_id": f"5h:{window_anchor}",
            }
        if not isinstance(subscription.metadata, dict):
            return {}
        existing_5h_anchor = subscription.metadata.get("code_plan_5h_anchor_epoch")
        if existing_5h_anchor is None:
            return {}
        preserved: dict[str, Any] = {"code_plan_5h_anchor_epoch": existing_5h_anchor}
        period_id = subscription.metadata.get("code_plan_5h_period_id")
        if period_id is not None:
            preserved["code_plan_5h_period_id"] = period_id
        return preserved

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

    def _week_anchor_epoch(self, subscription: SubscriptionRecord) -> int:
        for value in (subscription.window_week_start, subscription.renewed_at, subscription.created_at):
            epoch = self._epoch_or_none(value)
            if epoch is not None:
                return epoch
        return int(datetime.now(timezone.utc).timestamp())

    def _epoch_or_none(self, value: datetime | None) -> int | None:
        if value is None:
            return None
        dt = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())

    def _iso_or_none(self, value: object) -> str | None:
        if value is None:
            return None
        isoformat = getattr(value, "isoformat", None)
        if callable(isoformat):
            return str(isoformat())
        return str(value)
