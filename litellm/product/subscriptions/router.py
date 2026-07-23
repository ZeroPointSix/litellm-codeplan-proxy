from fastapi import APIRouter, Depends, HTTPException, Query, status

from litellm.product.credit_rules.repository import CreditRuleRepository
from litellm.product.plans.repository import PlanRepository
from litellm.product.subscriptions.litellm_client import ProxySubscriptionLiteLLMClient
from litellm.product.subscriptions.models import (
    SubscriptionActionRequest,
    SubscriptionCreateRequest,
    SubscriptionIssueKeyRequest,
    SubscriptionListResponse,
    SubscriptionProvisionResponse,
    SubscriptionRecord,
    SubscriptionRenewRequest,
    SubscriptionRevokeKeyRequest,
    SubscriptionStatus,
    SubscriptionUpgradeRequest,
)
from litellm.product.subscriptions.repository import SubscriptionRepository
from litellm.product.subscriptions.service import SubscriptionService
from litellm.proxy._types import LiteLLMRoutes, LitellmUserRoles, UserAPIKeyAuth
from litellm.proxy.auth.user_api_key_auth import user_api_key_auth

CODE_PLAN_SUBSCRIPTION_MANAGEMENT_ROUTES = [
    "/v1/admin/subscriptions",
    "/v1/admin/subscriptions/{subscription_id}",
    "/v1/admin/subscriptions/{subscription_id}/renew",
    "/v1/admin/subscriptions/{subscription_id}/pause",
    "/v1/admin/subscriptions/{subscription_id}/cancel",
    "/v1/admin/subscriptions/{subscription_id}/expire",
    "/v1/admin/subscriptions/{subscription_id}/upgrade",
    "/v1/admin/subscriptions/{subscription_id}/keys",
    "/v1/admin/subscriptions/{subscription_id}/keys/revoke",
]


def _register_management_routes() -> None:
    management_routes = LiteLLMRoutes.management_routes.value
    for route in CODE_PLAN_SUBSCRIPTION_MANAGEMENT_ROUTES:
        if route not in management_routes:
            management_routes.append(route)


_register_management_routes()

router = APIRouter(prefix="/v1/admin/subscriptions", tags=["Code Plan Subscription"])


def _require_proxy_admin(user_api_key_dict: UserAPIKeyAuth) -> None:
    user_role = getattr(user_api_key_dict, "user_role", None)
    admin_role = LitellmUserRoles.PROXY_ADMIN
    if user_role not in (admin_role, getattr(admin_role, "value", admin_role)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"error": "Proxy admin access required"})


def _get_service() -> SubscriptionService:
    from litellm.proxy.proxy_server import prisma_client

    if prisma_client is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail={"error": "Postgres DB Not connected"}
        )
    return SubscriptionService(
        SubscriptionRepository(prisma_client),
        PlanRepository(prisma_client),
        credit_rule_repository=CreditRuleRepository(prisma_client),
        litellm_client=ProxySubscriptionLiteLLMClient(),
    )


@router.post("", response_model=SubscriptionProvisionResponse)
async def create_subscription(
    data: SubscriptionCreateRequest,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
):
    _require_proxy_admin(user_api_key_dict)
    return await _get_service().create_subscription(data, actor=user_api_key_dict)


@router.get("", response_model=SubscriptionListResponse)
async def list_subscriptions(
    status_filter: SubscriptionStatus | None = Query(default=None, alias="status"),
    project_id: str | None = None,
    plan_id: str | None = None,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
):
    _require_proxy_admin(user_api_key_dict)
    subscriptions = await _get_service().list_subscriptions(
        status_filter=status_filter,
        project_id=project_id,
        plan_id=plan_id,
    )
    return SubscriptionListResponse(data=subscriptions)


@router.get("/{subscription_id}", response_model=SubscriptionRecord)
async def get_subscription(
    subscription_id: str,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
):
    _require_proxy_admin(user_api_key_dict)
    return await _get_service().get_subscription(subscription_id)


@router.post("/{subscription_id}/renew", response_model=SubscriptionProvisionResponse)
async def renew_subscription(
    subscription_id: str,
    data: SubscriptionRenewRequest,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
):
    _require_proxy_admin(user_api_key_dict)
    return await _get_service().renew_subscription(subscription_id, data, actor=user_api_key_dict)


@router.post("/{subscription_id}/pause", response_model=SubscriptionRecord)
async def pause_subscription(
    subscription_id: str,
    data: SubscriptionActionRequest,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
):
    _require_proxy_admin(user_api_key_dict)
    return await _get_service().pause_subscription(subscription_id, data, actor=user_api_key_dict)


@router.post("/{subscription_id}/cancel", response_model=SubscriptionRecord)
async def cancel_subscription(
    subscription_id: str,
    data: SubscriptionActionRequest,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
):
    _require_proxy_admin(user_api_key_dict)
    return await _get_service().cancel_subscription(subscription_id, data, actor=user_api_key_dict)


@router.post("/{subscription_id}/expire", response_model=SubscriptionRecord)
async def expire_subscription(
    subscription_id: str,
    data: SubscriptionActionRequest,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
):
    _require_proxy_admin(user_api_key_dict)
    return await _get_service().expire_subscription(subscription_id, data, actor=user_api_key_dict)


@router.post("/{subscription_id}/upgrade", response_model=SubscriptionProvisionResponse)
async def upgrade_subscription(
    subscription_id: str,
    data: SubscriptionUpgradeRequest,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
):
    _require_proxy_admin(user_api_key_dict)
    return await _get_service().upgrade_subscription(subscription_id, data, actor=user_api_key_dict)


@router.post("/{subscription_id}/keys", response_model=SubscriptionProvisionResponse)
async def issue_subscription_key(
    subscription_id: str,
    data: SubscriptionIssueKeyRequest,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
):
    _require_proxy_admin(user_api_key_dict)
    return await _get_service().issue_key(subscription_id, data, actor=user_api_key_dict)


@router.post("/{subscription_id}/keys/revoke", response_model=SubscriptionRecord)
async def revoke_subscription_key(
    subscription_id: str,
    data: SubscriptionRevokeKeyRequest,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
):
    _require_proxy_admin(user_api_key_dict)
    return await _get_service().revoke_key(subscription_id, data, actor=user_api_key_dict)
