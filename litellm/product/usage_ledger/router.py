from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from typing_extensions import Annotated

from litellm._logging import verbose_proxy_logger
from litellm.product.usage_ledger.models import (
    UsageLedgerAggregateResponse,
    UsageLedgerEventType,
    UsageLedgerGroupBy,
    UsageLedgerListResponse,
    UsageLedgerManualAdjustRequest,
)
from litellm.product.usage_ledger.repository import UsageLedgerRepository
from litellm.product.usage_ledger.service import UsageLedgerService
from litellm.proxy._types import LiteLLMRoutes, LitellmUserRoles, UserAPIKeyAuth
from litellm.proxy.auth.user_api_key_auth import user_api_key_auth

USAGE_LEDGER_MANAGEMENT_ROUTES = [
    "/v1/admin/usage-ledger",
    "/v1/admin/usage-ledger/summary",
    "/v1/admin/usage-ledger/manual-adjust",
]


def _register_management_routes() -> None:
    management_routes = LiteLLMRoutes.management_routes.value
    for route in USAGE_LEDGER_MANAGEMENT_ROUTES:
        if route not in management_routes:
            management_routes.append(route)


_register_management_routes()

router = APIRouter(prefix="/v1/admin/usage-ledger", tags=["Code Plan Usage Ledger"])

AdminUser = Annotated[UserAPIKeyAuth, Depends(user_api_key_auth)]


def _require_proxy_admin(user_api_key_dict: UserAPIKeyAuth) -> None:
    user_role = getattr(user_api_key_dict, "user_role", None)
    admin_role = LitellmUserRoles.PROXY_ADMIN
    if user_role not in (admin_role, getattr(admin_role, "value", admin_role)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "Only proxy admins can manage Code Plan usage ledger"},
        )


def _get_service() -> UsageLedgerService:
    from litellm.proxy.proxy_server import prisma_client

    if prisma_client is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "Database is not connected"},
        )
    return UsageLedgerService(UsageLedgerRepository(prisma_client))


@router.get("", response_model=UsageLedgerListResponse)
async def list_usage_ledger(
    user_api_key_dict: AdminUser,
    subscription_id: str | None = None,
    project_id: str | None = None,
    user_id: str | None = None,
    model: str | None = None,
    event_type: UsageLedgerEventType | None = None,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
) -> UsageLedgerListResponse:
    _require_proxy_admin(user_api_key_dict)
    service = _get_service()
    records = await service.list_events(
        subscription_id=subscription_id,
        project_id=project_id,
        user_id=user_id,
        model=model,
        event_type=event_type,
        start_time=start_time,
        end_time=end_time,
        limit=limit,
    )
    return UsageLedgerListResponse(data=records)


@router.get("/summary", response_model=UsageLedgerAggregateResponse)
async def summarize_usage_ledger(
    user_api_key_dict: AdminUser,
    group_by: UsageLedgerGroupBy = UsageLedgerGroupBy.DAY,
    subscription_id: str | None = None,
    project_id: str | None = None,
    user_id: str | None = None,
    model: str | None = None,
    event_type: UsageLedgerEventType | None = None,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
) -> UsageLedgerAggregateResponse:
    _require_proxy_admin(user_api_key_dict)
    service = _get_service()
    rows = await service.aggregate_events(
        group_by=group_by,
        subscription_id=subscription_id,
        project_id=project_id,
        user_id=user_id,
        model=model,
        event_type=event_type,
        start_time=start_time,
        end_time=end_time,
        limit=limit,
    )
    return UsageLedgerAggregateResponse(group_by=group_by, data=rows)


@router.post("/manual-adjust", response_model=UsageLedgerListResponse)
async def manual_adjust_usage_ledger(
    data: UsageLedgerManualAdjustRequest,
    user_api_key_dict: AdminUser,
) -> UsageLedgerListResponse:
    _require_proxy_admin(user_api_key_dict)
    try:
        record = await _get_service().manual_adjust(data)
    except (RuntimeError, TypeError, ValueError) as exc:
        verbose_proxy_logger.exception("Code Plan usage ledger manual adjustment failed: %s", exc)
        raise HTTPException(status_code=500, detail={"error": "Usage ledger manual adjustment failed"}) from exc
    return UsageLedgerListResponse(data=[record])
