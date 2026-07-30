import os
import uuid

import pytest
from fastapi.testclient import TestClient

from litellm.product.subscriptions.models import LiteLLMKeyProvision
from tests.proxy_unit_tests.code_plan_api_test_utils import build_code_plan_test_app


class FakeSubscriptionLiteLLMClient:
    team_counter = 0
    key_counter = 0
    revoked_keys: list[str] = []

    @classmethod
    def reset(cls):
        cls.team_counter = 0
        cls.key_counter = 0
        cls.revoked_keys = []

    async def create_team(self, config, actor=None):
        type(self).team_counter += 1
        return f"team-api-{type(self).team_counter}"

    async def update_team(self, team_id, config, actor=None):
        return None

    async def generate_key(self, team_id, subscription, key_alias=None, metadata=None, actor=None):
        type(self).key_counter += 1
        key_id = f"key-api-{type(self).key_counter}"
        return LiteLLMKeyProvision(key_id=key_id, key=f"sk-api-{type(self).key_counter}", token_id=key_id)

    async def revoke_key(self, key_id, actor=None):
        type(self).revoked_keys.append(key_id)


@pytest.fixture
def code_plan_client(monkeypatch):
    if os.getenv("CODE_PLAN_API_INTEGRATION") != "true":
        pytest.skip("CODE_PLAN_API_INTEGRATION not enabled")
    if not os.getenv("DATABASE_URL"):
        pytest.skip("DATABASE_URL not set")

    from litellm.product.subscriptions import router as subscription_router

    FakeSubscriptionLiteLLMClient.reset()
    monkeypatch.setattr(subscription_router, "ProxySubscriptionLiteLLMClient", FakeSubscriptionLiteLLMClient)

    app = build_code_plan_test_app()
    with TestClient(app) as client:
        yield client


def _plan_payload(**overrides):
    data = {
        "name": f"API Subscription Plan {uuid.uuid4().hex[:8]}",
        "quota_5h": 100,
        "quota_weekly": 1000,
        "allowed_models": ["gpt-4o-mini"],
        "max_keys": 2,
        "metadata": {"scope": "subscription-api-test"},
    }
    data.update(overrides)
    return data


def _create_active_plan(client: TestClient, **overrides):
    create_resp = client.post("/v1/admin/plans", json=_plan_payload(**overrides))
    assert create_resp.status_code == 200, create_resp.text
    draft = create_resp.json()

    activate_resp = client.post(f"/v1/admin/plans/{draft['plan_id']}/activate?version=1")
    assert activate_resp.status_code == 200, activate_resp.text
    return activate_resp.json()


def test_subscription_create_list_get_and_snapshot_freeze(code_plan_client: TestClient):
    plan = _create_active_plan(code_plan_client)
    project_id = f"project-{uuid.uuid4().hex[:8]}"

    create_resp = code_plan_client.post(
        "/v1/admin/subscriptions",
        json={"project_id": project_id, "plan_id": plan["plan_id"], "issue_key": False},
    )
    assert create_resp.status_code == 200, create_resp.text
    created = create_resp.json()["subscription"]
    assert created["version"] == 1
    assert created["litellm_team_id"] is None
    assert created["litellm_key_ids"] == []
    assert created["plan_snapshot"]["quota_5h"] == 100

    patch_resp = code_plan_client.patch(
        f"/v1/admin/plans/{plan['plan_id']}",
        json={"version": plan["version"], "quota_5h": 250},
    )
    assert patch_resp.status_code == 200, patch_resp.text

    get_resp = code_plan_client.get(f"/v1/admin/subscriptions/{created['subscription_id']}")
    assert get_resp.status_code == 200, get_resp.text
    stored = get_resp.json()
    assert stored["plan_snapshot"]["quota_5h"] == 100

    list_resp = code_plan_client.get(f"/v1/admin/subscriptions?project_id={project_id}")
    assert list_resp.status_code == 200, list_resp.text
    assert [row["subscription_id"] for row in list_resp.json()["data"]] == [created["subscription_id"]]


def test_subscription_key_lifecycle_api_flow(code_plan_client: TestClient):
    plan = _create_active_plan(code_plan_client)
    project_id = f"project-{uuid.uuid4().hex[:8]}"

    create_resp = code_plan_client.post(
        "/v1/admin/subscriptions",
        json={"project_id": project_id, "plan_id": plan["plan_id"], "key_alias": "primary"},
    )
    assert create_resp.status_code == 200, create_resp.text
    created = create_resp.json()
    subscription = created["subscription"]
    assert created["key_id"] == "key-api-1"
    assert created["key"] == "sk-api-1"
    assert subscription["litellm_team_id"] == "team-api-1"
    assert subscription["litellm_key_ids"] == ["key-api-1"]

    issue_resp = code_plan_client.post(
        f"/v1/admin/subscriptions/{subscription['subscription_id']}/keys",
        json={"version": subscription["version"], "key_alias": "secondary"},
    )
    assert issue_resp.status_code == 200, issue_resp.text
    with_second_key = issue_resp.json()["subscription"]
    assert with_second_key["litellm_key_ids"] == ["key-api-1", "key-api-2"]

    conflict_resp = code_plan_client.post(
        f"/v1/admin/subscriptions/{subscription['subscription_id']}/pause",
        json={"version": subscription["version"]},
    )
    assert conflict_resp.status_code == 409

    revoke_resp = code_plan_client.post(
        f"/v1/admin/subscriptions/{subscription['subscription_id']}/keys/revoke",
        json={"version": with_second_key["version"], "key_id": "key-api-1"},
    )
    assert revoke_resp.status_code == 200, revoke_resp.text
    after_revoke = revoke_resp.json()
    assert after_revoke["litellm_key_ids"] == ["key-api-2"]
    assert FakeSubscriptionLiteLLMClient.revoked_keys == ["key-api-1"]

    pause_resp = code_plan_client.post(
        f"/v1/admin/subscriptions/{subscription['subscription_id']}/pause",
        json={"version": after_revoke["version"]},
    )
    assert pause_resp.status_code == 200, pause_resp.text
    paused = pause_resp.json()
    assert paused["status"] == "paused"
    assert paused["litellm_key_ids"] == []
    assert FakeSubscriptionLiteLLMClient.revoked_keys == ["key-api-1", "key-api-2"]
