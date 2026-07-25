from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, status

from litellm.product.credit_rules.repository import CreditRuleRepository
from litellm.product.plans.models import PlanRecord, PlanStatus
from litellm.product.plans.repository import PlanRepository
from litellm.product.subscriptions.litellm_client import SubscriptionLiteLLMClient
from litellm.product.subscriptions.models import (
    LiteLLMKeyProvision,
    PlanSnapshot,
    SubscriptionActionRequest,
    SubscriptionCreateRequest,
    SubscriptionIssueKeyRequest,
    SubscriptionProvisionResponse,
    SubscriptionRecord,
    SubscriptionRenewRequest,
    SubscriptionRevokeKeyRequest,
    SubscriptionStatus,
    SubscriptionUpgradeRequest,
)
from litellm.product.subscriptions.repository import SubscriptionRepository
from litellm.product.subscriptions.snapshot import plan_snapshot_from_plan, snapshot_to_litellm_team_config
from litellm.proxy._types import UserAPIKeyAuth


class SubscriptionService:
    def __init__(
        self,
        repository: SubscriptionRepository,
        plan_repository: PlanRepository,
        credit_rule_repository: CreditRuleRepository | None = None,
        litellm_client: SubscriptionLiteLLMClient | None = None,
    ):
        self.repository = repository
        self.plan_repository = plan_repository
        self.credit_rule_repository = credit_rule_repository
        self.litellm_client = litellm_client

    async def create_subscription(
        self,
        data: SubscriptionCreateRequest,
        actor: UserAPIKeyAuth | None = None,
    ) -> SubscriptionProvisionResponse:
        self._validate_future_expiry(data.expires_at)
        plan = await self._require_active_plan(data.plan_id)
        snapshot = plan_snapshot_from_plan(plan, await self._active_credit_rule(plan))
        record = await self.repository.create(
            {
                "project_id": data.project_id,
                "plan_id": plan.plan_id,
                "status": SubscriptionStatus.ACTIVE.value,
                "plan_snapshot": snapshot,
                "litellm_team_id": None,
                "litellm_key_ids": [],
                "expires_at": data.expires_at,
                "renewed_at": None,
                "paused_at": None,
                "canceled_at": None,
                "metadata": data.metadata,
                "version": 1,
                "created_by": self._actor_id(actor),
                "updated_by": self._actor_id(actor),
            }
        )
        if not data.issue_key:
            return SubscriptionProvisionResponse(subscription=record)
        return await self._issue_key_for_record(record, data.key_alias, data.metadata, actor=actor)

    async def list_subscriptions(
        self,
        status_filter: SubscriptionStatus | None = None,
        project_id: str | None = None,
        plan_id: str | None = None,
    ) -> list[SubscriptionRecord]:
        status_value = status_filter.value if status_filter else None
        return await self.repository.list(status=status_value, project_id=project_id, plan_id=plan_id)

    async def get_subscription(self, subscription_id: str) -> SubscriptionRecord:
        record = await self.repository.get(subscription_id)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "Subscription not found"})
        return record

    async def renew_subscription(
        self,
        subscription_id: str,
        data: SubscriptionRenewRequest,
        actor: UserAPIKeyAuth | None = None,
    ) -> SubscriptionProvisionResponse:
        existing = await self.get_subscription(subscription_id)
        self._require_version(existing, data.version)
        if existing.status == SubscriptionStatus.CANCELED:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail={"error": "Canceled subscriptions cannot renew"}
            )

        expires_at = data.expires_at if "expires_at" in data.model_fields_set else existing.expires_at
        self._validate_future_expiry(expires_at)
        updated = await self._update_existing(
            existing,
            {
                "status": SubscriptionStatus.ACTIVE.value,
                "expires_at": expires_at,
                "renewed_at": self._now(),
                "paused_at": None,
                "canceled_at": None,
                "metadata": self._merge_metadata(existing.metadata, data.metadata),
                "version": existing.version + 1,
                "updated_by": self._actor_id(actor),
            },
        )
        updated = await self._sync_litellm_team(updated, actor=actor)
        if not data.issue_key:
            return SubscriptionProvisionResponse(subscription=updated)
        return await self._issue_key_for_record(updated, data.key_alias, data.metadata, actor=actor)

    async def pause_subscription(
        self,
        subscription_id: str,
        data: SubscriptionActionRequest,
        actor: UserAPIKeyAuth | None = None,
    ) -> SubscriptionRecord:
        existing = await self.get_subscription(subscription_id)
        self._require_version(existing, data.version)
        if existing.status != SubscriptionStatus.ACTIVE:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail={"error": "Only active subscriptions can pause"}
            )
        await self._revoke_keys(existing.litellm_key_ids, actor=actor)
        return await self._update_existing(
            existing,
            {
                "status": SubscriptionStatus.PAUSED.value,
                "litellm_key_ids": [],
                "paused_at": self._now(),
                "version": existing.version + 1,
                "updated_by": self._actor_id(actor),
            },
        )

    async def cancel_subscription(
        self,
        subscription_id: str,
        data: SubscriptionActionRequest,
        actor: UserAPIKeyAuth | None = None,
    ) -> SubscriptionRecord:
        existing = await self.get_subscription(subscription_id)
        self._require_version(existing, data.version)
        if existing.status == SubscriptionStatus.CANCELED:
            return existing
        await self._revoke_keys(existing.litellm_key_ids, actor=actor)
        return await self._update_existing(
            existing,
            {
                "status": SubscriptionStatus.CANCELED.value,
                "litellm_key_ids": [],
                "canceled_at": self._now(),
                "version": existing.version + 1,
                "updated_by": self._actor_id(actor),
            },
        )

    async def expire_subscription(
        self,
        subscription_id: str,
        data: SubscriptionActionRequest,
        actor: UserAPIKeyAuth | None = None,
    ) -> SubscriptionRecord:
        existing = await self.get_subscription(subscription_id)
        self._require_version(existing, data.version)
        if existing.status in (SubscriptionStatus.EXPIRED, SubscriptionStatus.CANCELED):
            return existing
        await self._revoke_keys(existing.litellm_key_ids, actor=actor)
        return await self._update_existing(
            existing,
            {
                "status": SubscriptionStatus.EXPIRED.value,
                "litellm_key_ids": [],
                "version": existing.version + 1,
                "updated_by": self._actor_id(actor),
            },
        )

    async def issue_key(
        self,
        subscription_id: str,
        data: SubscriptionIssueKeyRequest,
        actor: UserAPIKeyAuth | None = None,
    ) -> SubscriptionProvisionResponse:
        existing = await self.get_subscription(subscription_id)
        self._require_version(existing, data.version)
        return await self._issue_key_for_record(existing, data.key_alias, data.metadata, actor=actor)

    async def revoke_key(
        self,
        subscription_id: str,
        data: SubscriptionRevokeKeyRequest,
        actor: UserAPIKeyAuth | None = None,
    ) -> SubscriptionRecord:
        existing = await self.get_subscription(subscription_id)
        self._require_version(existing, data.version)
        if data.key_id not in existing.litellm_key_ids:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "Subscription key not found"})
        await self._revoke_keys([data.key_id], actor=actor)
        return await self._update_existing(
            existing,
            {
                "litellm_key_ids": [key_id for key_id in existing.litellm_key_ids if key_id != data.key_id],
                "version": existing.version + 1,
                "updated_by": self._actor_id(actor),
            },
        )

    async def upgrade_subscription(
        self,
        subscription_id: str,
        data: SubscriptionUpgradeRequest,
        actor: UserAPIKeyAuth | None = None,
    ) -> SubscriptionProvisionResponse:
        existing = await self.get_subscription(subscription_id)
        self._require_version(existing, data.version)
        self._require_active_subscription(existing)
        new_plan = await self._require_active_plan(data.plan_id)
        self._require_upgrade(existing.plan_snapshot, new_plan)

        expires_at = data.expires_at if "expires_at" in data.model_fields_set else existing.expires_at
        self._validate_future_expiry(expires_at)
        await self._revoke_keys(existing.litellm_key_ids, actor=actor)
        canceled = await self._update_existing(
            existing,
            {
                "status": SubscriptionStatus.CANCELED.value,
                "litellm_key_ids": [],
                "canceled_at": self._now(),
                "version": existing.version + 1,
                "updated_by": self._actor_id(actor),
            },
        )
        snapshot = plan_snapshot_from_plan(new_plan, await self._active_credit_rule(new_plan))
        new_subscription = await self.repository.create(
            {
                "project_id": canceled.project_id,
                "plan_id": new_plan.plan_id,
                "status": SubscriptionStatus.ACTIVE.value,
                "plan_snapshot": snapshot,
                "litellm_team_id": canceled.litellm_team_id,
                "litellm_key_ids": [],
                "expires_at": expires_at,
                "renewed_at": self._now(),
                "paused_at": None,
                "canceled_at": None,
                "metadata": self._merge_metadata(canceled.metadata, data.metadata),
                "version": 1,
                "created_by": self._actor_id(actor),
                "updated_by": self._actor_id(actor),
            }
        )
        new_subscription = await self._sync_litellm_team(new_subscription, actor=actor)
        if not data.issue_key:
            return SubscriptionProvisionResponse(subscription=new_subscription)
        return await self._issue_key_for_record(new_subscription, data.key_alias, data.metadata, actor=actor)

    async def _issue_key_for_record(
        self,
        record: SubscriptionRecord,
        key_alias: str | None,
        metadata: dict[str, Any] | None,
        actor: UserAPIKeyAuth | None = None,
    ) -> SubscriptionProvisionResponse:
        self._require_active_subscription(record)
        self._require_not_expired(record)
        if len(record.litellm_key_ids) >= record.plan_snapshot.max_keys:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail={"error": "Subscription max_keys exceeded"}
            )
        record = await self._sync_litellm_team(record, actor=actor)
        if self.litellm_client is None or not record.litellm_team_id:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail={"error": "LiteLLM client unavailable"}
            )

        provision = await self.litellm_client.generate_key(
            team_id=record.litellm_team_id,
            subscription=record,
            key_alias=key_alias or self._default_key_alias(record),
            metadata=metadata,
            actor=actor,
        )
        updated = await self._update_existing(
            record,
            {
                "litellm_key_ids": [*record.litellm_key_ids, provision.key_id],
                "version": record.version + 1,
                "updated_by": self._actor_id(actor),
            },
        )
        return self._provision_response(updated, provision)

    async def _sync_litellm_team(
        self,
        record: SubscriptionRecord,
        actor: UserAPIKeyAuth | None = None,
    ) -> SubscriptionRecord:
        if self.litellm_client is None:
            if record.litellm_team_id:
                return record
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail={"error": "LiteLLM client unavailable"}
            )
        config = self._team_config(record)
        if record.litellm_team_id:
            await self.litellm_client.update_team(record.litellm_team_id, config, actor=actor)
            return record

        team_id = await self.litellm_client.create_team(config, actor=actor)
        return await self._update_existing(
            record,
            {"litellm_team_id": team_id, "version": record.version + 1, "updated_by": self._actor_id(actor)},
        )

    def _team_config(self, record: SubscriptionRecord) -> dict[str, Any]:
        config = snapshot_to_litellm_team_config(record.plan_snapshot)
        config["team_alias"] = f"{record.project_id}:{record.plan_snapshot.name}"[:255]
        metadata = dict(config.get("metadata") or {})
        metadata.update(
            {
                "code_plan_subscription_id": record.subscription_id,
                "code_plan_project_id": record.project_id,
                "code_plan_subscription_version": record.version,
            }
        )
        config["metadata"] = metadata
        return config

    async def _revoke_keys(self, key_ids: list[str], actor: UserAPIKeyAuth | None = None) -> None:
        if not key_ids:
            return
        if self.litellm_client is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail={"error": "LiteLLM client unavailable"}
            )
        for key_id in key_ids:
            await self.litellm_client.revoke_key(key_id, actor=actor)

    async def _require_active_plan(self, plan_id: str) -> PlanRecord:
        plan = await self.plan_repository.get(plan_id)
        if plan is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "Plan not found"})
        if plan.status != PlanStatus.ACTIVE:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail={"error": "Subscription requires an active Plan"}
            )
        return plan

    async def _active_credit_rule(self, plan: PlanRecord) -> Any | None:
        if not plan.credit_rule_id or self.credit_rule_repository is None:
            return None
        rule = await self.credit_rule_repository.get_active(plan.credit_rule_id)
        if rule is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "plan credit_rule_id must reference an active CreditRule"},
            )
        return rule

    async def _update_existing(self, existing: SubscriptionRecord, data: dict[str, Any]) -> SubscriptionRecord:
        updated = await self.repository.update_if_version(existing.subscription_id, existing.version, data)
        if updated is None:
            current = await self.repository.get(existing.subscription_id)
            current_version = current.version if current else existing.version
            self._raise_version_conflict(current_version)
        return updated

    def _require_version(self, record: SubscriptionRecord, version: int) -> None:
        if record.version != version:
            self._raise_version_conflict(record.version)

    def _raise_version_conflict(self, current_version: int) -> None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "Subscription version conflict", "current_version": current_version},
        )

    def _require_active_subscription(self, record: SubscriptionRecord) -> None:
        if record.status != SubscriptionStatus.ACTIVE:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"error": "Subscription is not active"})

    def _require_not_expired(self, record: SubscriptionRecord) -> None:
        if record.expires_at is not None and self._utc(record.expires_at) <= self._now():
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"error": "Subscription expired"})

    def _validate_future_expiry(self, expires_at: datetime | None) -> None:
        if expires_at is not None and self._utc(expires_at) <= self._now():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail={"error": "expires_at must be in the future"}
            )

    def _require_upgrade(self, current: PlanSnapshot, new_plan: PlanRecord) -> None:
        if self._is_downgrade(current, new_plan):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail={"error": "Plan downgrade is not allowed"}
            )
        if current.plan_id == new_plan.plan_id and current.plan_version == new_plan.version:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail={"error": "Subscription is already on this Plan"}
            )

    def _is_downgrade(self, current: PlanSnapshot, new_plan: PlanRecord) -> bool:
        if new_plan.quota_5h < current.quota_5h or new_plan.quota_weekly < current.quota_weekly:
            return True
        if new_plan.max_keys < current.max_keys:
            return True
        if not set(current.allowed_models).issubset(set(new_plan.allowed_models)):
            return True
        return any(
            self._optional_limit_downgrade(getattr(new_plan, field_name), getattr(current, field_name))
            for field_name in ("rpm_limit", "tpm_limit", "max_parallel_requests", "default_max_output_tokens")
        )

    def _optional_limit_downgrade(self, new_value: int | None, current_value: int | None) -> bool:
        if current_value is None:
            return new_value is not None
        if new_value is None:
            return False
        return new_value < current_value

    def _merge_metadata(self, current: dict[str, Any], patch: dict[str, Any] | None) -> dict[str, Any]:
        merged = dict(current)
        merged.update(patch or {})
        return merged

    def _default_key_alias(self, record: SubscriptionRecord) -> str:
        return f"code-plan-{record.subscription_id}-{len(record.litellm_key_ids) + 1}"

    def _provision_response(
        self,
        subscription: SubscriptionRecord,
        provision: LiteLLMKeyProvision,
    ) -> SubscriptionProvisionResponse:
        return SubscriptionProvisionResponse(
            subscription=subscription,
            key_id=provision.key_id,
            key=provision.key,
            token_id=provision.token_id,
        )

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def _utc(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _actor_id(self, actor: UserAPIKeyAuth | None) -> str | None:
        if actor is None:
            return None
        for field_name in ("user_id", "key_alias", "token"):
            value = getattr(actor, field_name, None)
            if value:
                return str(value)
        return None
