from typing import Any, Optional

from fastapi import HTTPException, status
from pydantic import ValidationError

from litellm.product.plans.models import (
    PlanCreateRequest,
    PlanEntitlements,
    PlanPatchRequest,
    PlanRecord,
    PlanStatus,
)
from litellm.product.plans.repository import PlanRepository
from litellm.proxy._types import UserAPIKeyAuth


class PlanService:
    def __init__(self, repository: PlanRepository):
        self.repository = repository

    async def create_plan(self, data: PlanCreateRequest, actor: Optional[UserAPIKeyAuth] = None) -> PlanRecord:
        payload = data.model_dump()
        payload.update(
            {
                "status": PlanStatus.DRAFT.value,
                "version": 1,
                "created_by": self._actor_id(actor),
                "updated_by": self._actor_id(actor),
            }
        )
        return await self.repository.create(payload)

    async def list_plans(self, status_filter: Optional[PlanStatus] = None) -> list[PlanRecord]:
        status_value = status_filter.value if status_filter else None
        return await self.repository.list(status=status_value)

    async def get_plan(self, plan_id: str) -> PlanRecord:
        plan = await self.repository.get(plan_id)
        if plan is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "Plan not found"})
        return plan

    async def patch_plan(
        self,
        plan_id: str,
        data: PlanPatchRequest,
        actor: Optional[UserAPIKeyAuth] = None,
    ) -> PlanRecord:
        existing = await self.get_plan(plan_id)
        self._require_version(existing, data.version)
        if existing.status == PlanStatus.ARCHIVED:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail={"error": "Archived plans cannot be modified"}
            )

        merged = existing.model_dump()
        patch = data.model_dump(exclude={"version"}, exclude_unset=True)
        if "metadata" in patch and patch["metadata"] is None:
            patch["metadata"] = {}
        merged.update(patch)
        self._validate_entitlements(merged)

        patch.update({"version": existing.version + 1, "updated_by": self._actor_id(actor)})
        return await self.repository.update(plan_id, patch)

    async def activate_plan(
        self, plan_id: str, version: Optional[int] = None, actor: Optional[UserAPIKeyAuth] = None
    ) -> PlanRecord:
        existing = await self.get_plan(plan_id)
        if version is not None:
            self._require_version(existing, version)
        if existing.status != PlanStatus.DRAFT:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail={"error": "Only draft plans can be activated"}
            )
        if not existing.allowed_models:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "allowed_models must be non-empty before activation"},
            )

        return await self.repository.update(
            plan_id,
            {
                "status": PlanStatus.ACTIVE.value,
                "version": existing.version + 1,
                "updated_by": self._actor_id(actor),
            },
        )

    async def archive_plan(
        self, plan_id: str, version: Optional[int] = None, actor: Optional[UserAPIKeyAuth] = None
    ) -> PlanRecord:
        existing = await self.get_plan(plan_id)
        if version is not None:
            self._require_version(existing, version)
        if existing.status == PlanStatus.ARCHIVED:
            return existing
        if existing.status != PlanStatus.ACTIVE:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail={"error": "Only active plans can be archived"}
            )

        return await self.repository.update(
            plan_id,
            {
                "status": PlanStatus.ARCHIVED.value,
                "version": existing.version + 1,
                "updated_by": self._actor_id(actor),
            },
        )

    def _require_version(self, plan: PlanRecord, version: int) -> None:
        if plan.version != version:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "Plan version conflict", "current_version": plan.version},
            )

    def _validate_entitlements(self, data: dict[str, Any]) -> None:
        entitlement_data = {
            field_name: data[field_name] for field_name in PlanEntitlements.model_fields if field_name in data
        }
        try:
            PlanEntitlements.model_validate(entitlement_data)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors()) from exc

    def _actor_id(self, actor: Optional[UserAPIKeyAuth]) -> Optional[str]:
        if actor is None:
            return None
        for field_name in ("user_id", "key_alias", "token"):
            value = getattr(actor, field_name, None)
            if value:
                return str(value)
        return None
