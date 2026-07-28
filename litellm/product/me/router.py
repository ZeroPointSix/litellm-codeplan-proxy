from __future__ import annotations

import os
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from litellm.product.credit_rules.repository import CreditRuleRepository
from litellm.product.me.models import (
    PortalIntegrationResponse,
    PortalKeyCreateRequest,
    PortalKeyCreateResponse,
    PortalKeyListResponse,
    PortalKeyRevokeResponse,
    PortalQuotaResponse,
    PortalRequestListResponse,
    PortalSubscriptionResponse,
    PortalUsageResponse,
)
from litellm.product.me.repository import PortalRepository
from litellm.product.me.service import PortalService
from litellm.product.plans.repository import PlanRepository
from litellm.product.quotas.redis_store import RedisQuotaStore
from litellm.product.subscriptions.litellm_client import ProxySubscriptionLiteLLMClient
from litellm.product.subscriptions.repository import SubscriptionRepository
from litellm.product.subscriptions.service import SubscriptionService
from litellm.proxy._types import LiteLLMRoutes, UserAPIKeyAuth
from litellm.proxy.auth.user_api_key_auth import user_api_key_auth


PORTAL_SELF_MANAGED_ROUTES = [
    "/v1/me/subscription",
    "/v1/me/quota",
    "/v1/me/usage",
    "/v1/me/requests",
    "/v1/me/keys",
    "/v1/me/keys/{key_id}",
    "/v1/me/integration",
]

for route in PORTAL_SELF_MANAGED_ROUTES:
    if route not in LiteLLMRoutes.self_managed_routes.value:
        LiteLLMRoutes.self_managed_routes.value.append(route)

router = APIRouter(prefix="/v1/me", tags=["Code Plan Portal"])
PortalUser = Annotated[UserAPIKeyAuth, Depends(user_api_key_auth)]


def _get_service(require_quota_reader: bool = False) -> PortalService:
    from litellm.proxy.proxy_server import prisma_client

    if prisma_client is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "Postgres DB Not connected"},
        )
    subscription_repository = SubscriptionRepository(prisma_client)
    plan_repository = PlanRepository(prisma_client)
    return PortalService(
        subscription_repository=subscription_repository,
        subscription_service=SubscriptionService(
            repository=subscription_repository,
            plan_repository=plan_repository,
            credit_rule_repository=CreditRuleRepository(prisma_client),
            litellm_client=ProxySubscriptionLiteLLMClient(),
        ),
        portal_repository=PortalRepository(prisma_client),
        quota_reader=_get_quota_reader(required=require_quota_reader),
    )


def _get_quota_reader(required: bool) -> RedisQuotaStore | None:
    redis_url = os.getenv("CODE_PLAN_QUOTA_REDIS_URL") or os.getenv("REDIS_URL")
    if not redis_url:
        if required:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Code Plan quota Redis is not configured",
            )
        return None
    import redis.asyncio as redis

    return RedisQuotaStore(redis.from_url(redis_url, decode_responses=True))


@router.get("/subscription", response_model=PortalSubscriptionResponse)
async def get_my_subscription(user_api_key_dict: PortalUser) -> PortalSubscriptionResponse:
    return await _get_service().get_subscription(user_api_key_dict)


@router.get("/quota", response_model=PortalQuotaResponse)
async def get_my_quota(
    user_api_key_dict: PortalUser,
    timezone_name: str = Query(default="UTC", alias="timezone"),
) -> PortalQuotaResponse:
    return await _get_service(require_quota_reader=True).get_quota(
        user_api_key_dict=user_api_key_dict,
        timezone_name=timezone_name,
    )


@router.get("/usage", response_model=PortalUsageResponse)
async def get_my_usage(
    user_api_key_dict: PortalUser,
    timezone_name: str = Query(default="UTC", alias="timezone"),
    start_time: datetime | None = Query(default=None),
    end_time: datetime | None = Query(default=None),
) -> PortalUsageResponse:
    return await _get_service().get_usage(
        user_api_key_dict=user_api_key_dict,
        timezone_name=timezone_name,
        start_time=start_time,
        end_time=end_time,
    )


@router.get("/requests", response_model=PortalRequestListResponse)
async def get_my_requests(
    user_api_key_dict: PortalUser,
    start_time: datetime | None = Query(default=None),
    end_time: datetime | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
) -> PortalRequestListResponse:
    return await _get_service().get_requests(
        user_api_key_dict=user_api_key_dict,
        start_time=start_time,
        end_time=end_time,
        page=page,
        page_size=page_size,
    )


@router.get("/keys", response_model=PortalKeyListResponse)
async def list_my_keys(user_api_key_dict: PortalUser) -> PortalKeyListResponse:
    return await _get_service().list_keys(user_api_key_dict)


@router.post("/keys", response_model=PortalKeyCreateResponse)
async def create_my_key(
    request: PortalKeyCreateRequest,
    user_api_key_dict: PortalUser,
) -> PortalKeyCreateResponse:
    return await _get_service().create_key(user_api_key_dict, request)


@router.delete("/keys", response_model=PortalKeyRevokeResponse)
async def revoke_my_key_by_query(
    user_api_key_dict: PortalUser,
    key_id: str = Query(..., min_length=1),
) -> PortalKeyRevokeResponse:
    return await _get_service().revoke_key(user_api_key_dict, key_id)


@router.delete("/keys/{key_id}", response_model=PortalKeyRevokeResponse)
async def revoke_my_key(
    key_id: str,
    user_api_key_dict: PortalUser,
) -> PortalKeyRevokeResponse:
    return await _get_service().revoke_key(user_api_key_dict, key_id)


@router.get("/integration", response_model=PortalIntegrationResponse)
async def get_my_integration(
    request: Request,
    user_api_key_dict: PortalUser,
) -> PortalIntegrationResponse:
    base_url = os.getenv("PROXY_BASE_URL") or str(request.base_url).rstrip("/")
    return await _get_service().get_integration(user_api_key_dict, base_url)
