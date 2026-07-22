from fastapi import APIRouter, Depends, HTTPException, Query, status

from litellm.product.plans.models import (
    PlanCreateRequest,
    PlanListResponse,
    PlanPatchRequest,
    PlanRecord,
    PlanStatus,
)
from litellm.product.plans.repository import PlanRepository
from litellm.product.plans.service import PlanService
from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth
from litellm.proxy.auth.user_api_key_auth import user_api_key_auth

router = APIRouter(prefix="/v1/admin/plans", tags=["Code Plan"])


def _require_proxy_admin(user_api_key_dict: UserAPIKeyAuth) -> None:
    user_role = getattr(user_api_key_dict, "user_role", None)
    admin_role = LitellmUserRoles.PROXY_ADMIN
    if user_role not in (admin_role, getattr(admin_role, "value", admin_role)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"error": "Proxy admin access required"})


def _get_service() -> PlanService:
    from litellm.proxy.proxy_server import prisma_client

    if prisma_client is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail={"error": "Postgres DB Not connected"}
        )
    return PlanService(PlanRepository(prisma_client))


@router.post("", response_model=PlanRecord)
async def create_plan(
    data: PlanCreateRequest,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
):
    _require_proxy_admin(user_api_key_dict)
    return await _get_service().create_plan(data, actor=user_api_key_dict)


@router.get("", response_model=PlanListResponse)
async def list_plans(
    status_filter: PlanStatus | None = Query(default=None, alias="status"),
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
):
    _require_proxy_admin(user_api_key_dict)
    plans = await _get_service().list_plans(status_filter=status_filter)
    return PlanListResponse(data=plans)


@router.get("/{plan_id}", response_model=PlanRecord)
async def get_plan(
    plan_id: str,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
):
    _require_proxy_admin(user_api_key_dict)
    return await _get_service().get_plan(plan_id)


@router.patch("/{plan_id}", response_model=PlanRecord)
async def patch_plan(
    plan_id: str,
    data: PlanPatchRequest,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
):
    _require_proxy_admin(user_api_key_dict)
    return await _get_service().patch_plan(plan_id, data, actor=user_api_key_dict)


@router.post("/{plan_id}/activate", response_model=PlanRecord)
async def activate_plan(
    plan_id: str,
    version: int | None = Query(default=None, ge=1),
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
):
    _require_proxy_admin(user_api_key_dict)
    return await _get_service().activate_plan(plan_id, version=version, actor=user_api_key_dict)


@router.post("/{plan_id}/archive", response_model=PlanRecord)
async def archive_plan(
    plan_id: str,
    version: int | None = Query(default=None, ge=1),
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
):
    _require_proxy_admin(user_api_key_dict)
    return await _get_service().archive_plan(plan_id, version=version, actor=user_api_key_dict)
