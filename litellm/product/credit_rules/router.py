from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from litellm.product.credit_rules.models import (
    CreditRuleCreateRequest,
    CreditRuleListResponse,
    CreditRulePatchRequest,
    CreditRuleRecord,
    CreditRuleStatus,
)
from litellm.product.credit_rules.repository import CreditRuleRepository
from litellm.product.credit_rules.service import CreditRuleService
from litellm.proxy._types import LiteLLMRoutes, LitellmUserRoles, UserAPIKeyAuth
from litellm.proxy.auth.user_api_key_auth import user_api_key_auth

CREDIT_RULE_MANAGEMENT_ROUTES = [
    "/v1/admin/credit-rules",
    "/v1/admin/credit-rules/{credit_rule_id}",
    "/v1/admin/credit-rules/{credit_rule_id}/activate",
    "/v1/admin/credit-rules/{credit_rule_id}/archive",
]


def _register_management_routes() -> None:
    management_routes = LiteLLMRoutes.management_routes.value
    for route in CREDIT_RULE_MANAGEMENT_ROUTES:
        if route not in management_routes:
            management_routes.append(route)


_register_management_routes()

router = APIRouter(prefix="/v1/admin/credit-rules", tags=["Code Plan"])

AdminUser = Annotated[UserAPIKeyAuth, Depends(user_api_key_auth)]
StatusQuery = Annotated[CreditRuleStatus | None, Query(alias="status")]
CreditRuleIdFilterQuery = Annotated[str | None, Query()]
VersionQuery = Annotated[int | None, Query(ge=1)]


def _require_proxy_admin(user_api_key_dict: UserAPIKeyAuth) -> None:
    user_role = getattr(user_api_key_dict, "user_role", None)
    admin_role = LitellmUserRoles.PROXY_ADMIN
    if user_role not in (admin_role, getattr(admin_role, "value", admin_role)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"error": "Proxy admin access required"})


def _get_service() -> CreditRuleService:
    from litellm.proxy.proxy_server import prisma_client

    if prisma_client is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail={"error": "Postgres DB Not connected"}
        )
    return CreditRuleService(CreditRuleRepository(prisma_client))


@router.post("", response_model=CreditRuleRecord)
async def create_credit_rule(
    data: CreditRuleCreateRequest,
    user_api_key_dict: AdminUser,
):
    _require_proxy_admin(user_api_key_dict)
    return await _get_service().create_rule(data, actor=user_api_key_dict)


@router.get("", response_model=CreditRuleListResponse)
async def list_credit_rules(
    user_api_key_dict: AdminUser,
    status_filter: StatusQuery = None,
    credit_rule_id: CreditRuleIdFilterQuery = None,
):
    _require_proxy_admin(user_api_key_dict)
    rules = await _get_service().list_rules(status_filter=status_filter, credit_rule_id=credit_rule_id)
    return CreditRuleListResponse(data=rules)


@router.get("/{credit_rule_id}", response_model=CreditRuleRecord)
async def get_credit_rule(
    credit_rule_id: str,
    user_api_key_dict: AdminUser,
    version: VersionQuery = None,
):
    _require_proxy_admin(user_api_key_dict)
    return await _get_service().get_rule(credit_rule_id, version=version)


@router.patch("/{credit_rule_id}", response_model=CreditRuleRecord)
async def patch_credit_rule(
    credit_rule_id: str,
    data: CreditRulePatchRequest,
    user_api_key_dict: AdminUser,
):
    _require_proxy_admin(user_api_key_dict)
    return await _get_service().patch_rule(credit_rule_id, data, actor=user_api_key_dict)


@router.post("/{credit_rule_id}/activate", response_model=CreditRuleRecord)
async def activate_credit_rule(
    credit_rule_id: str,
    user_api_key_dict: AdminUser,
    version: VersionQuery = None,
):
    _require_proxy_admin(user_api_key_dict)
    return await _get_service().activate_rule(credit_rule_id, version=version, actor=user_api_key_dict)


@router.post("/{credit_rule_id}/archive", response_model=CreditRuleRecord)
async def archive_credit_rule(
    credit_rule_id: str,
    user_api_key_dict: AdminUser,
    version: VersionQuery = None,
):
    _require_proxy_admin(user_api_key_dict)
    return await _get_service().archive_rule(credit_rule_id, version=version, actor=user_api_key_dict)
