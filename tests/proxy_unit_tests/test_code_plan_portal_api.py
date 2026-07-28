from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from litellm.product.quotas.models import QuotaWindow
from litellm.product.subscriptions.litellm_client import CODE_PLAN_GATEWAY_ALLOWED_ROUTES
from litellm.product.subscriptions.models import LiteLLMKeyProvision, SubscriptionRecord
from litellm.proxy import proxy_server
from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth
from litellm.proxy.auth import user_api_key_auth as auth_module
from tests.proxy_unit_tests.code_plan_api_test_utils import build_code_plan_test_app


class FakePortalSubscriptionLiteLLMClient:
    team_counter: int = 0
    key_counter: int = 0
    revoked_keys: list[str] = []

    @classmethod
    def reset(cls) -> None:
        cls.team_counter = 0
        cls.key_counter = 0
        cls.revoked_keys = []

    async def create_team(self, config: dict[str, object], actor: UserAPIKeyAuth | None = None) -> str:
        type(self).team_counter += 1
        return f"team-portal-{type(self).team_counter}"

    async def update_team(
        self,
        team_id: str,
        config: dict[str, object],
        actor: UserAPIKeyAuth | None = None,
    ) -> None:
        return None

    async def generate_key(
        self,
        team_id: str,
        subscription: SubscriptionRecord,
        key_alias: str | None = None,
        metadata: dict[str, object] | None = None,
        actor: UserAPIKeyAuth | None = None,
    ) -> LiteLLMKeyProvision:
        type(self).key_counter += 1
        key_id = f"key-portal-{type(self).key_counter}"
        key_metadata = dict(metadata or {})
        key_metadata.update(
            {
                "code_plan_subscription_id": subscription.subscription_id,
                "code_plan_project_id": subscription.project_id,
            }
        )
        prisma_client = proxy_server.prisma_client
        assert prisma_client is not None
        await prisma_client.db.execute_raw(
            """INSERT INTO "LiteLLM_VerificationToken" (
                    token,
                    key_alias,
                    models,
                    team_id,
                    metadata,
                    blocked,
                    allowed_routes,
                    created_at,
                    updated_at
                )
                VALUES ($1, $2, $3::text[], $4, $5::jsonb, false, $6::text[], NOW(), NOW())
                ON CONFLICT (token) DO NOTHING""",
            key_id,
            key_alias,
            list(subscription.plan_snapshot.allowed_models),
            team_id,
            json.dumps(key_metadata),
            list(CODE_PLAN_GATEWAY_ALLOWED_ROUTES),
        )
        return LiteLLMKeyProvision(key_id=key_id, key=f"sk-portal-{type(self).key_counter}", token_id=key_id)

    async def revoke_key(self, key_id: str, actor: UserAPIKeyAuth | None = None) -> None:
        type(self).revoked_keys.append(key_id)
        prisma_client = proxy_server.prisma_client
        assert prisma_client is not None
        await prisma_client.db.execute_raw("""DELETE FROM "LiteLLM_VerificationToken" WHERE token = $1""", key_id)


class FakeQuotaReader:
    async def resolve_windows(self, subscription_id: str, windows: list[QuotaWindow]) -> list[QuotaWindow]:
        return windows

    async def get_window_spent(self, subscription_id: str, windows: list[QuotaWindow]) -> dict[str, float]:
        return {"5h": 12.5, "week": 150.0}


class SpendLogSeed(BaseModel):
    request_id: str
    api_key: str
    team_id: str
    subscription_id: str
    project_id: str
    start_time: datetime
    end_time: datetime


@dataclass
class AuthState:
    user: UserAPIKeyAuth


async def _current_auth(auth_state: AuthState) -> UserAPIKeyAuth:
    return auth_state.user


def _admin_user() -> UserAPIKeyAuth:
    return UserAPIKeyAuth(
        api_key="sk-test-code-plan-admin",
        user_role=LitellmUserRoles.PROXY_ADMIN,
        user_id="code-plan-portal-admin",
    )


def _portal_user(project_id: str) -> UserAPIKeyAuth:
    return UserAPIKeyAuth.model_construct(
        api_key="jwt-portal-user",
        user_id="portal-user",
        jwt_claims={"project_id": project_id, "sub": "portal-user"},
    )


def _non_jwt_user() -> UserAPIKeyAuth:
    return UserAPIKeyAuth.model_construct(api_key="sk-non-jwt", user_id="portal-user")


def _install_spend_log_seed_route(app: FastAPI) -> None:
    @app.post("/_test/code-plan/spend-log")
    async def seed_spend_log(seed: SpendLogSeed) -> dict[str, str]:
        prisma_client = proxy_server.prisma_client
        assert prisma_client is not None
        await prisma_client.db.execute_raw(
            """INSERT INTO "LiteLLM_SpendLogs" (
                    request_id,
                    call_type,
                    api_key,
                    spend,
                    total_tokens,
                    prompt_tokens,
                    completion_tokens,
                    "startTime",
                    "endTime",
                    request_duration_ms,
                    model,
                    api_base,
                    metadata,
                    team_id,
                    status,
                    cache_hit
                )
                VALUES ($1, 'completion', $2, 99.0, 15, 10, 5, $3, $4, 125.0, 'gpt-4o-mini',
                        'https://api.test', $5::jsonb, $6, 'success', 'false')""",
            seed.request_id,
            seed.api_key,
            seed.start_time,
            seed.end_time,
            json.dumps(
                {
                    "code_plan_subscription_id": seed.subscription_id,
                    "code_plan_project_id": seed.project_id,
                }
            ),
            seed.team_id,
        )
        return {"request_id": seed.request_id}


@pytest.fixture
def portal_client(monkeypatch: pytest.MonkeyPatch):
    if os.getenv("CODE_PLAN_API_INTEGRATION") != "true":
        pytest.skip("CODE_PLAN_API_INTEGRATION not enabled")
    if not os.getenv("DATABASE_URL"):
        pytest.skip("DATABASE_URL not set")

    from litellm.product.me import router as me_router
    from litellm.product.subscriptions import router as subscription_router

    FakePortalSubscriptionLiteLLMClient.reset()
    monkeypatch.setattr(subscription_router, "ProxySubscriptionLiteLLMClient", FakePortalSubscriptionLiteLLMClient)
    monkeypatch.setattr(me_router, "ProxySubscriptionLiteLLMClient", FakePortalSubscriptionLiteLLMClient)
    monkeypatch.setattr(me_router, "_get_quota_reader", lambda required: FakeQuotaReader())
    monkeypatch.setenv("PROXY_BASE_URL", "https://portal.example.test")

    app = build_code_plan_test_app()
    _install_spend_log_seed_route(app)
    auth_state = AuthState(user=_admin_user())

    async def current_auth() -> UserAPIKeyAuth:
        return await _current_auth(auth_state)

    app.dependency_overrides[auth_module.user_api_key_auth] = current_auth
    with TestClient(app) as client:
        yield client, auth_state


def _plan_payload(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "name": f"Portal API Plan {uuid.uuid4().hex[:8]}",
        "quota_5h": 100,
        "quota_weekly": 1000,
        "allowed_models": ["gpt-4o-mini"],
        "max_keys": 3,
        "metadata": {"scope": "portal-api-test"},
    }
    data.update(overrides)
    return data


def _create_active_plan(client: TestClient, **overrides: object) -> dict[str, object]:
    create_resp = client.post("/v1/admin/plans", json=_plan_payload(**overrides))
    assert create_resp.status_code == 200, create_resp.text
    draft = create_resp.json()

    activate_resp = client.post(f"/v1/admin/plans/{draft['plan_id']}/activate?version=1")
    assert activate_resp.status_code == 200, activate_resp.text
    return activate_resp.json()


def _create_subscription(client: TestClient, plan: dict[str, object], project_id: str) -> tuple[dict[str, object], str]:
    create_resp = client.post(
        "/v1/admin/subscriptions",
        json={"project_id": project_id, "plan_id": plan["plan_id"], "key_alias": "primary"},
    )
    assert create_resp.status_code == 200, create_resp.text
    created = create_resp.json()
    return created["subscription"], created["key_id"]


def test_portal_requires_jwt_and_active_subscription(portal_client: tuple[TestClient, AuthState]) -> None:
    client, auth_state = portal_client
    auth_state.user = _non_jwt_user()
    response = client.get("/v1/me/subscription")
    assert response.status_code == 403

    auth_state.user = _portal_user(f"project-{uuid.uuid4().hex[:8]}")
    response = client.get("/v1/me/subscription")
    assert response.status_code == 404


def test_portal_api_flow(portal_client: tuple[TestClient, AuthState]) -> None:
    client, auth_state = portal_client
    project_id = f"project-{uuid.uuid4().hex[:8]}"
    plan = _create_active_plan(client)
    subscription, primary_key_id = _create_subscription(client, plan, project_id)
    assert primary_key_id == "key-portal-1"

    started = datetime.now(timezone.utc) - timedelta(hours=1)
    ended = started + timedelta(minutes=1)
    seed_resp = client.post(
        "/_test/code-plan/spend-log",
        json={
            "request_id": f"req-{uuid.uuid4().hex}",
            "api_key": primary_key_id,
            "team_id": subscription["litellm_team_id"],
            "subscription_id": subscription["subscription_id"],
            "project_id": project_id,
            "start_time": started.isoformat(),
            "end_time": ended.isoformat(),
        },
    )
    assert seed_resp.status_code == 200, seed_resp.text

    auth_state.user = _portal_user(project_id)
    range_params = {
        "start_time": (started - timedelta(minutes=1)).isoformat(),
        "end_time": (ended + timedelta(minutes=1)).isoformat(),
        "timezone": "UTC",
    }

    subscription_resp = client.get("/v1/me/subscription")
    assert subscription_resp.status_code == 200, subscription_resp.text
    portal_subscription = subscription_resp.json()
    assert portal_subscription["subscription_id"] == subscription["subscription_id"]
    assert portal_subscription["project_id"] == project_id
    assert portal_subscription["key_count"] == 1

    quota_resp = client.get("/v1/me/quota", params={"timezone": "UTC"})
    assert quota_resp.status_code == 200, quota_resp.text
    quota = quota_resp.json()
    assert quota["windows"][0]["used"] == 12.5
    assert quota["windows"][1]["used"] == 150.0

    usage_resp = client.get("/v1/me/usage", params=range_params)
    assert usage_resp.status_code == 200, usage_resp.text
    usage = usage_resp.json()
    assert usage["totals"]["request_count"] == 1
    assert usage["totals"]["external_credits"] == 15.0
    assert "spend" not in usage_resp.text

    requests_resp = client.get("/v1/me/requests", params=range_params)
    assert requests_resp.status_code == 200, requests_resp.text
    requests = requests_resp.json()
    assert requests["total"] == 1
    assert requests["data"][0]["external_credits"] == 15.0
    assert "spend" not in requests_resp.text

    keys_resp = client.get("/v1/me/keys")
    assert keys_resp.status_code == 200, keys_resp.text
    assert [row["key_id"] for row in keys_resp.json()["data"]] == [primary_key_id]

    key_resp = client.post("/v1/me/keys", json={"key_alias": "secondary"})
    assert key_resp.status_code == 200, key_resp.text
    secondary_key_id = key_resp.json()["key_id"]
    assert secondary_key_id == "key-portal-2"
    assert key_resp.json()["key"] == "sk-portal-2"

    query_revoke_resp = client.delete("/v1/me/keys", params={"key_id": primary_key_id})
    assert query_revoke_resp.status_code == 200, query_revoke_resp.text
    assert query_revoke_resp.json()["revoked_key_id"] == primary_key_id

    path_revoke_resp = client.delete(f"/v1/me/keys/{secondary_key_id}")
    assert path_revoke_resp.status_code == 200, path_revoke_resp.text
    assert path_revoke_resp.json()["revoked_key_id"] == secondary_key_id
    assert FakePortalSubscriptionLiteLLMClient.revoked_keys == [primary_key_id, secondary_key_id]

    integration_resp = client.get("/v1/me/integration")
    assert integration_resp.status_code == 200, integration_resp.text
    integration = integration_resp.json()
    assert integration["api_base_url"] == "https://portal.example.test/v1"
    assert integration["allowed_routes"] == CODE_PLAN_GATEWAY_ALLOWED_ROUTES
    assert integration["sample_config"]["openai"]["base_url"] == "https://portal.example.test/v1"
