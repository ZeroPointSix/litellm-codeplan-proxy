from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from litellm.product.credit_rules.router import router as credit_rule_router
from litellm.product.plans.router import router as plan_router
from litellm.product.subscriptions.router import router as subscription_router
from litellm.proxy import proxy_server
from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth
from litellm.proxy.auth import user_api_key_auth as auth_module
from litellm.proxy.common_utils.user_api_key_cache import UserApiKeyCache
from litellm.proxy.utils import PrismaClient, ProxyLogging

_prisma_client: PrismaClient | None = None


async def fake_admin_auth() -> UserAPIKeyAuth:
    return UserAPIKeyAuth(
        api_key="sk-test-code-plan",
        user_role=LitellmUserRoles.PROXY_ADMIN,
        user_id="code-plan-api-test",
    )


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    global _prisma_client
    database_url = os.environ["DATABASE_URL"]
    cache = UserApiKeyCache()
    _prisma_client = PrismaClient(
        database_url=database_url,
        proxy_logging_obj=ProxyLogging(user_api_key_cache=cache),
    )
    await _prisma_client.connect()
    proxy_server.prisma_client = _prisma_client
    proxy_server.user_api_key_cache = cache
    try:
        yield
    finally:
        if _prisma_client is not None:
            await _prisma_client.disconnect()
        proxy_server.prisma_client = None
        _prisma_client = None


def build_code_plan_test_app() -> FastAPI:
    app = FastAPI(lifespan=_lifespan)
    app.include_router(credit_rule_router)
    app.include_router(plan_router)
    app.include_router(subscription_router)
    app.dependency_overrides[auth_module.user_api_key_auth] = fake_admin_auth
    return app
