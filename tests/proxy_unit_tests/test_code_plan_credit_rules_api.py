import os
import uuid

import pytest
from fastapi.testclient import TestClient

from tests.proxy_unit_tests.code_plan_api_test_utils import build_code_plan_test_app


@pytest.fixture
def code_plan_client():
    if os.getenv("CODE_PLAN_API_INTEGRATION") != "true":
        pytest.skip("CODE_PLAN_API_INTEGRATION not enabled")
    if not os.getenv("DATABASE_URL"):
        pytest.skip("DATABASE_URL not set")

    app = build_code_plan_test_app()
    with TestClient(app) as client:
        yield client


def _rule_payload(**overrides):
    data = {
        "name": f"API Test Rule {uuid.uuid4().hex[:8]}",
        "input_multiplier": 1.0,
        "output_multiplier": 4.0,
        "cache_read_multiplier": 0.1,
        "cache_write_multiplier": 1.25,
        "metadata": {"scope": "ci-api-test"},
    }
    data.update(overrides)
    return data


def test_credit_rule_create_patch_activate_archive_flow(code_plan_client: TestClient):
    create_resp = code_plan_client.post("/v1/admin/credit-rules", json=_rule_payload())
    assert create_resp.status_code == 200, create_resp.text
    draft = create_resp.json()
    assert draft["status"] == "draft"
    assert draft["version"] == 1

    rule_id = draft["credit_rule_id"]
    patch_resp = code_plan_client.patch(
        f"/v1/admin/credit-rules/{rule_id}",
        json={"version": 1, "output_multiplier": 5.0},
    )
    assert patch_resp.status_code == 200, patch_resp.text
    second = patch_resp.json()
    assert second["version"] == 2
    assert second["status"] == "draft"
    assert second["output_multiplier"] == 5.0

    history_resp = code_plan_client.get(f"/v1/admin/credit-rules?credit_rule_id={rule_id}")
    assert history_resp.status_code == 200, history_resp.text
    versions = {row["version"] for row in history_resp.json()["data"]}
    assert versions == {1, 2}

    activate_resp = code_plan_client.post(f"/v1/admin/credit-rules/{rule_id}/activate?version=2")
    assert activate_resp.status_code == 200, activate_resp.text
    active = activate_resp.json()
    assert active["status"] == "active"
    assert active["version"] == 2

    archive_resp = code_plan_client.post(f"/v1/admin/credit-rules/{rule_id}/archive?version=2")
    assert archive_resp.status_code == 200, archive_resp.text
    assert archive_resp.json()["status"] == "archived"


def test_credit_rule_rejects_all_zero_multipliers(code_plan_client: TestClient):
    response = code_plan_client.post(
        "/v1/admin/credit-rules",
        json=_rule_payload(
            name=f"Zero Rule {uuid.uuid4().hex[:8]}",
            input_multiplier=0,
            output_multiplier=0,
            cache_read_multiplier=0,
            cache_write_multiplier=0,
        ),
    )
    assert response.status_code == 422


def test_plan_requires_active_credit_rule(code_plan_client: TestClient):
    create_resp = code_plan_client.post("/v1/admin/credit-rules", json=_rule_payload())
    assert create_resp.status_code == 200, create_resp.text
    draft_rule = create_resp.json()

    invalid_plan = code_plan_client.post(
        "/v1/admin/plans",
        json={
            "name": f"Invalid Plan {uuid.uuid4().hex[:8]}",
            "quota_5h": 100,
            "quota_weekly": 1000,
            "allowed_models": ["gpt-4o-mini"],
            "credit_rule_id": draft_rule["credit_rule_id"],
        },
    )
    assert invalid_plan.status_code == 400
    assert invalid_plan.json()["detail"]["error"] == "credit_rule_id must reference an active CreditRule"

    activate_resp = code_plan_client.post(
        f"/v1/admin/credit-rules/{draft_rule['credit_rule_id']}/activate?version=1"
    )
    assert activate_resp.status_code == 200, activate_resp.text

    valid_plan = code_plan_client.post(
        "/v1/admin/plans",
        json={
            "name": f"Valid Plan {uuid.uuid4().hex[:8]}",
            "quota_5h": 100,
            "quota_weekly": 1000,
            "allowed_models": ["gpt-4o-mini"],
            "credit_rule_id": draft_rule["credit_rule_id"],
        },
    )
    assert valid_plan.status_code == 200, valid_plan.text
    plan = valid_plan.json()
    assert plan["credit_rule_id"] == draft_rule["credit_rule_id"]

    archive_resp = code_plan_client.post(
        f"/v1/admin/credit-rules/{draft_rule['credit_rule_id']}/archive?version=1"
    )
    assert archive_resp.status_code == 200, archive_resp.text

    activate_plan = code_plan_client.post(f"/v1/admin/plans/{plan['plan_id']}/activate?version=1")
    assert activate_plan.status_code == 400
    assert activate_plan.json()["detail"]["error"] == "credit_rule_id must reference an active CreditRule"
