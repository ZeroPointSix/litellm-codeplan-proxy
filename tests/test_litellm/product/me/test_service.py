from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from litellm.product.me.models import PortalKeyCreateRequest
from litellm.product.me.service import PortalService
from litellm.product.subscriptions.models import PlanSnapshot, SubscriptionRecord
from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth


class FakeSubscriptionRepository:
    def __init__(self, record: SubscriptionRecord | None):
        self.record = record
        self.calls: list[tuple[list[str] | None, str | None]] = []

    async def get_current_for_scope(
        self,
        project_ids: list[str] | None = None,
        litellm_team_id: str | None = None,
    ) -> SubscriptionRecord | None:
        self.calls.append((project_ids, litellm_team_id))
        return self.record


class FakeSubscriptionService:
    def __init__(self, record: SubscriptionRecord):
        self.record = record
        self.issue_call = None
        self.revoke_call = None

    async def issue_key(self, subscription_id: str, data, actor=None):
        self.issue_call = (subscription_id, data, actor)
        updated = self.record.model_copy(update={"litellm_key_ids": [*self.record.litellm_key_ids, "key-hash"], "version": 2})
        return SimpleNamespace(subscription=updated, key_id="key-hash", token_id="token-id", key="sk-user")

    async def revoke_key(self, subscription_id: str, data, actor=None):
        self.revoke_call = (subscription_id, data, actor)
        return self.record.model_copy(update={"litellm_key_ids": [], "version": 2})


class FakePortalRepository:
    async def list_keys(self, subscription: SubscriptionRecord):
        return []

    async def usage_buckets(self, subscription: SubscriptionRecord, start_time, end_time, timezone_name: str):
        return [
            {
                "date": "2026-07-28",
                "request_count": 2,
                "input_tokens": 10,
                "output_tokens": 5,
                "total_tokens": 15,
                "spend": 999,
            }
        ]

    async def request_count(self, subscription: SubscriptionRecord, start_time, end_time):
        return 1

    async def request_rows(self, subscription: SubscriptionRecord, start_time, end_time, page: int, page_size: int):
        return [
            {
                "request_id": "req-1",
                "start_time": start_time,
                "end_time": end_time,
                "request_duration_ms": 123,
                "model": "gpt-4o-mini",
                "status": "success",
                "input_tokens": 3,
                "output_tokens": 4,
                "total_tokens": 7,
                "spend": 999,
            }
        ]


class FakeQuotaReader:
    async def resolve_windows(self, subscription_id: str, windows):
        return windows

    async def get_window_spent(self, subscription_id: str, windows):
        return {"5h": 12.5, "week": 150.0}


def make_record() -> SubscriptionRecord:
    now = datetime.now(timezone.utc) - timedelta(days=1)
    return SubscriptionRecord(
        subscription_id="sub-1",
        project_id="project-a",
        plan_id="plan-a",
        plan_snapshot=PlanSnapshot(
            plan_id="plan-a",
            name="Team Plan",
            plan_version=1,
            quota_5h=100,
            quota_weekly=1000,
            allowed_models=["gpt-4o-mini"],
            max_keys=2,
            credit_input_multiplier=1.0,
            credit_output_multiplier=2.0,
        ),
        litellm_team_id="team-a",
        litellm_key_ids=["key-1"],
        version=1,
        created_at=now,
    )


def make_user(claims: dict[str, object] | None = None) -> UserAPIKeyAuth:
    return UserAPIKeyAuth.model_construct(
        api_key="jwt-user",
        jwt_claims=claims,
        project_id=None,
        team_id=None,
        user_id="user-a",
        user_email=None,
    )


def make_service(record: SubscriptionRecord | None = None, quota_reader=None) -> tuple[PortalService, FakeSubscriptionRepository, FakeSubscriptionService]:
    record = record or make_record()
    subscription_repository = FakeSubscriptionRepository(record)
    subscription_service = FakeSubscriptionService(record)
    service = PortalService(
        subscription_repository=subscription_repository,
        subscription_service=subscription_service,
        portal_repository=FakePortalRepository(),
        quota_reader=quota_reader,
    )
    return service, subscription_repository, subscription_service


@pytest.mark.asyncio
async def test_subscription_requires_jwt():
    service, _, _ = make_service()

    with pytest.raises(HTTPException) as exc_info:
        await service.get_subscription(make_user())

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "JWT auth required"


@pytest.mark.asyncio
async def test_subscription_uses_jwt_project_scope():
    service, repository, _ = make_service()

    response = await service.get_subscription(make_user({"project_id": "project-a"}))

    assert repository.calls == [(["project-a"], None)]
    assert response.subscription.subscription_id == "sub-1"
    assert response.subscription.key_count == 1
    assert "spend" not in response.model_dump_json()


@pytest.mark.asyncio
async def test_quota_reports_dual_window_remaining_in_timezone():
    service, _, _ = make_service(quota_reader=FakeQuotaReader())

    response = await service.get_quota(make_user({"project_id": "project-a"}), "America/New_York")
    windows = {window.name: window for window in response.windows}

    assert response.timezone == "America/New_York"
    assert windows["5h"].used == 12.5
    assert windows["5h"].remaining == 87.5
    assert windows["week"].used == 150.0
    assert windows["week"].remaining == 850.0
    assert windows["week"].anchor_at_local is not None


@pytest.mark.asyncio
async def test_usage_and_requests_return_external_credits_without_spend():
    service, _, _ = make_service()
    start_time = datetime(2026, 7, 1, tzinfo=timezone.utc)
    end_time = datetime(2026, 7, 29, tzinfo=timezone.utc)

    usage = await service.get_usage(make_user({"project_id": "project-a"}), "UTC", start_time, end_time)
    requests = await service.get_requests(make_user({"project_id": "project-a"}), start_time, end_time, 1, 50)

    assert usage.totals.external_credits == 20.0
    assert requests.data[0].external_credits == 11.0
    assert "spend" not in usage.model_dump_json()
    assert "spend" not in requests.model_dump_json()


@pytest.mark.asyncio
async def test_create_and_revoke_key_use_internal_admin_actor():
    service, _, subscription_service = make_service()

    created = await service.create_key(
        make_user({"project_id": "project-a", "sub": "user-a"}),
        PortalKeyCreateRequest(key_alias="portal-key"),
    )
    revoked = await service.revoke_key(make_user({"project_id": "project-a", "sub": "user-a"}), "key-1")

    assert created.key == "sk-user"
    assert created.token_id == "token-id"
    assert revoked.revoked_key_id == "key-1"
    assert subscription_service.issue_call[2].user_role == LitellmUserRoles.PROXY_ADMIN
    assert subscription_service.revoke_call[2].user_role == LitellmUserRoles.PROXY_ADMIN


def test_subscription_router_mounts_portal_paths():
    from litellm.product.subscriptions.router import router

    paths = {route.path for route in router.routes}

    assert "/v1/admin/subscriptions" in paths
    assert "/v1/me/subscription" in paths
    assert "/v1/me/keys/{key_id}" in paths
