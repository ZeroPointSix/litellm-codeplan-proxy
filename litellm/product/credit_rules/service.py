from typing import Any

from fastapi import HTTPException, status
from pydantic import ValidationError

from litellm.product.credit_rules.models import (
    CreditRuleCreateRequest,
    CreditRuleMultipliers,
    CreditRulePatchRequest,
    CreditRuleRecord,
    CreditRuleStatus,
)
from litellm.product.credit_rules.repository import CreditRuleRepository
from litellm.proxy._types import UserAPIKeyAuth


class CreditRuleService:
    def __init__(self, repository: CreditRuleRepository):
        self.repository = repository

    async def create_rule(self, data: CreditRuleCreateRequest, actor: UserAPIKeyAuth | None = None) -> CreditRuleRecord:
        payload = data.model_dump()
        payload.update(
            {
                "status": CreditRuleStatus.DRAFT.value,
                "version": 1,
                "created_by": self._actor_id(actor),
                "updated_by": self._actor_id(actor),
            }
        )
        return await self.repository.create(payload)

    async def list_rules(
        self, status_filter: CreditRuleStatus | None = None, credit_rule_id: str | None = None
    ) -> list[CreditRuleRecord]:
        status_value = status_filter.value if status_filter else None
        return await self.repository.list(status=status_value, credit_rule_id=credit_rule_id)

    async def get_rule(self, credit_rule_id: str, version: int | None = None) -> CreditRuleRecord:
        rule = await self.repository.get(credit_rule_id, version=version)
        if rule is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "CreditRule not found"})
        return rule

    async def patch_rule(
        self,
        credit_rule_id: str,
        data: CreditRulePatchRequest,
        actor: UserAPIKeyAuth | None = None,
    ) -> CreditRuleRecord:
        existing = await self.get_rule(credit_rule_id)
        self._require_version(existing, data.version)
        if existing.status == CreditRuleStatus.ARCHIVED:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail={"error": "Archived credit rules cannot be modified"}
            )

        patch = data.model_dump(exclude={"version"}, exclude_unset=True)
        if "metadata" in patch and patch["metadata"] is None:
            patch["metadata"] = {}
        merged = existing.model_dump()
        merged.update(patch)
        self._validate_multipliers(merged)

        payload = self._version_payload(existing=existing, merged=merged, actor=actor)
        return await self.repository.create(payload)

    async def activate_rule(
        self, credit_rule_id: str, version: int | None = None, actor: UserAPIKeyAuth | None = None
    ) -> CreditRuleRecord:
        existing = await self.get_rule(credit_rule_id, version=version)
        if existing.status == CreditRuleStatus.ACTIVE:
            return existing
        if existing.status != CreditRuleStatus.DRAFT:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail={"error": "Only draft credit rules can be activated"}
            )

        actor_id = self._actor_id(actor)
        await self.repository.archive_active_versions(credit_rule_id, actor_id=actor_id)
        updated = await self.repository.update_status(
            credit_rule_id, existing.version, CreditRuleStatus.ACTIVE.value, actor_id=actor_id
        )
        if updated is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "CreditRule version conflict", "current_version": existing.version},
            )
        return updated

    async def archive_rule(
        self, credit_rule_id: str, version: int | None = None, actor: UserAPIKeyAuth | None = None
    ) -> CreditRuleRecord:
        existing = await self.get_rule(credit_rule_id, version=version)
        if existing.status == CreditRuleStatus.ARCHIVED:
            return existing

        updated = await self.repository.update_status(
            credit_rule_id, existing.version, CreditRuleStatus.ARCHIVED.value, actor_id=self._actor_id(actor)
        )
        if updated is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "CreditRule version conflict", "current_version": existing.version},
            )
        return updated

    async def require_active_rule(self, credit_rule_id: str) -> CreditRuleRecord:
        rule = await self.repository.get_active(credit_rule_id)
        if rule is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "credit_rule_id must reference an active CreditRule"},
            )
        return rule

    def _version_payload(
        self,
        existing: CreditRuleRecord,
        merged: dict[str, Any],
        actor: UserAPIKeyAuth | None = None,
    ) -> dict[str, Any]:
        return {
            "credit_rule_id": existing.credit_rule_id,
            "name": merged["name"],
            "description": merged.get("description"),
            "status": CreditRuleStatus.DRAFT.value,
            "input_multiplier": merged["input_multiplier"],
            "output_multiplier": merged["output_multiplier"],
            "cache_read_multiplier": merged["cache_read_multiplier"],
            "cache_write_multiplier": merged["cache_write_multiplier"],
            "effective_at": merged.get("effective_at"),
            "metadata": merged.get("metadata") or {},
            "version": existing.version + 1,
            "created_by": self._actor_id(actor),
            "updated_by": self._actor_id(actor),
        }

    def _require_version(self, rule: CreditRuleRecord, version: int) -> None:
        if rule.version != version:
            self._raise_version_conflict(rule.version)

    def _raise_version_conflict(self, current_version: int) -> None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "CreditRule version conflict", "current_version": current_version},
        )

    def _validate_multipliers(self, data: dict[str, Any]) -> None:
        multiplier_data = {
            field_name: data[field_name] for field_name in CreditRuleMultipliers.model_fields if field_name in data
        }
        try:
            CreditRuleMultipliers.model_validate(multiplier_data)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors()) from exc

    def _actor_id(self, actor: UserAPIKeyAuth | None) -> str | None:
        if actor is None:
            return None
        for field_name in ("user_id", "key_alias", "token"):
            value = getattr(actor, field_name, None)
            if value:
                return str(value)
        return None
