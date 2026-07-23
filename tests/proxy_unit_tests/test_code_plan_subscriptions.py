from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi import HTTPException

from litellm.product.plans.models import PlanRecord, PlanStatus
from litellm.product.subscriptions.models import (
    LiteLLMKeyProvision,
    SubscriptionActionRequest,
    SubscriptionCreateRequest,
    SubscriptionIssueKeyRequest,
    SubscriptionRecord,
    SubscriptionRenewRequest,
    SubscriptionRevokeKeyRequest,
    SubscriptionStatus,
    SubscriptionUpgradeRequest,
)
from litellm.product.subscriptions.repository import SubscriptionRepository
from litellm.product.subscriptions.router import CODE_PLAN_SUBSCRIPTION_MANAGEMENT_ROUTES
from litellm.product.subscriptions.service import SubscriptionService
from litellm.product.subscriptions.snapshot import plan_snapshot_from_plan
from litellm.proxy._types import LiteLLMRoutes
from litellm.proxy.auth.route_checks import RouteChecks


class InMemoryPlanRepository:
    def __init__(self, records: list[PlanRecord] | None = None):
        self.records = {record.plan_id: record for record in records or []}

    async def get(self, plan_id):
        return self.records.get(plan_id)

    async def list(self, status=None):
        records = list(self.records.values())
        if status:
            records = [record for record in records if record.status == status]
        return records

    async def update(self, plan_id, data):
        existing = self.records[plan_id]
        merged = existing.model_dump()
        merged.update(data)
        updated = PlanRecord.model_validate(merged)
        self.records[plan_id] = updated
        return updated

    async def update_if_version(self, plan_id, version, data):
        existing = self.records[plan_id]
        if existing.version != version:
            return None
        return await self.update(plan_id, data)


class InMemorySubscriptionRepository:
    def __init__(self):
        self.records: dict[str, SubscriptionRecord] = {}
        self.counter = 0

    async def create(self, data):
        self.counter += 1
        now = datetime.now(timezone.utc)
        record = SubscriptionRecord(
            subscription_id=f"sub-{self.counter}",
            created_at=now,
            updated_at=now,
            **data,
        )
        self.records[record.subscription_id] = record
        return record

    async def get(self, subscription_id):
        return self.records.get(subscription_id)

    async def list(self, status=None, project_id=None, plan_id=None):
        records = list(self.records.values())
        if status:
            records = [record for record in records if record.status == status]
        if project_id:
            records = [record for record in records if record.project_id == project_id]
        if plan_id:
            records = [record for record in records if record.plan_id == plan_id]
        return records

    async def update(self, subscription_id, data):
        existing = self.records[subscription_id]
        merged = existing.model_dump()
        merged.update(data)
        merged["updated_at"] = datetime.now(timezone.utc)
        updated = SubscriptionRecord.model_validate(merged)
        self.records[subscription_id] = updated
        return updated

    async def update_if_version(self, subscription_id, version, data):
        existing = self.records[subscription_id]
        if existing.version != version:
            return None
        return await self.update(subscription_id, data)


class FakeLiteLLMClient:
    def __init__(self):
        self.team_counter = 0
        self.key_counter = 0
        self.created_teams: dict[str, dict[str, Any]] = {}
        self.updated_teams: list[tuple[str, dict[str, Any]]] = []
        self.generated_keys: list[tuple[str, str | None, dict[str, Any] | None]] = []
        self.revoked_keys: list[str] = []

    async def create_team(self, config, actor=None):
        self.team_counter += 1
        team_id = f"team-{self.team_counter}"
        self.created_teams[team_id] = config
        return team_id

    async def update_team(self, team_id, config, actor=None):
        self.updated_teams.append((team_id, config))

    async def generate_key(self, team_id, subscription, key_alias=None, metadata=None, actor=None):
        self.key_counter += 1
        key_id = f"key-{self.key_counter}"
        self.generated_keys.append((team_id, key_alias, metadata))
        return LiteLLMKeyProvision(key_id=key_id, key=f"sk-test-{self.key_counter}", token_id=key_id)

    async def revoke_key(self, key_id, actor=None):
        self.revoked_keys.append(key_id)


class ReplicaReadActions:
    async def find_unique(self, *args, **kwargs):
        raise AssertionError("update_if_version must not read the replica after a successful write")


class RecordingWriter:
    def __init__(self, row):
        self.row = row
        self.calls = []

    async def query_raw(self, query, *args):
        self.calls.append((query, args))
        return [self.row]


class RawUpdateDB:
    def __init__(self, row):
        self.writer = RecordingWriter(row)
        self.litellm_codeplansubscriptiontable = ReplicaReadActions()


class RawUpdatePrismaClient:
    def __init__(self, row):
        self.db = RawUpdateDB(row)


def _plan(**overrides) -> PlanRecord:
    data = {
        "plan_id": "plan-basic",
        "name": "Basic Code",
        "description": None,
        "status": PlanStatus.ACTIVE,
        "version": 1,
        "quota_5h": 100,
        "quota_weekly": 1000,
        "allowed_models": ["anthropic/claude-4-sonnet", "gpt-4.1"],
        "rpm_limit": 60,
        "tpm_limit": 120000,
        "max_parallel_requests": 4,
        "max_keys": 5,
        "default_max_output_tokens": 4096,
        "credit_rule_id": None,
        "metadata": {"tier": "basic"},
    }
    data.update(overrides)
    return PlanRecord(**data)


def _service(plan_records: list[PlanRecord] | None = None, client: FakeLiteLLMClient | None = None):
    plan_repository = InMemoryPlanRepository(plan_records or [_plan()])
    repository = InMemorySubscriptionRepository()
    litellm_client = client or FakeLiteLLMClient()
    return SubscriptionService(repository, plan_repository, litellm_client=litellm_client), repository, plan_repository, litellm_client


def test_code_plan_subscription_routes_are_management_routes():
    management_routes = LiteLLMRoutes.management_routes.value
    for route in CODE_PLAN_SUBSCRIPTION_MANAGEMENT_ROUTES:
        assert route in management_routes

    assert RouteChecks.check_route_access(route="/v1/admin/subscriptions", allowed_routes=management_routes)
    assert RouteChecks.check_route_access(route="/v1/admin/subscriptions/sub-1", allowed_routes=management_routes)
    assert RouteChecks.check_route_access(route="/v1/admin/subscriptions/sub-1/renew", allowed_routes=management_routes)
    assert RouteChecks.check_route_access(route="/v1/admin/subscriptions/sub-1/keys", allowed_routes=management_routes)
    assert RouteChecks.check_route_access(route="/v1/admin/subscriptions/sub-1/keys/revoke", allowed_routes=management_routes)


@pytest.mark.asyncio
async def test_create_subscription_freezes_snapshot_and_provisions_team_key():
    service, _, plan_repository, litellm_client = _service()

    response = await service.create_subscription(SubscriptionCreateRequest(project_id="project-1", plan_id="plan-basic"))

    subscription = response.subscription
    assert subscription.status == SubscriptionStatus.ACTIVE
    assert subscription.version == 3
    assert subscription.plan_snapshot.quota_5h == 100
    assert subscription.plan_snapshot.allowed_models == ["anthropic/claude-4-sonnet", "gpt-4.1"]
    assert subscription.litellm_team_id == "team-1"
    assert subscription.litellm_key_ids == ["key-1"]
    assert response.key == "sk-test-1"
    assert response.key_id == "key-1"

    await plan_repository.update("plan-basic", {"quota_5h": 999, "version": 2})
    stored = await service.get_subscription(subscription.subscription_id)

    assert stored.plan_snapshot.quota_5h == 100
    assert litellm_client.created_teams["team-1"]["metadata"]["code_plan_subscription_id"] == subscription.subscription_id
    assert litellm_client.generated_keys[0][0] == "team-1"


@pytest.mark.asyncio
async def test_create_subscription_without_key_keeps_skeleton_only():
    service, _, _, litellm_client = _service()

    response = await service.create_subscription(
        SubscriptionCreateRequest(
            project_id="project-1",
            plan_id="plan-basic",
            issue_key=False,
            metadata={"phase": "s1"},
        )
    )

    subscription = response.subscription
    assert response.key is None
    assert response.key_id is None
    assert subscription.version == 1
    assert subscription.litellm_team_id is None
    assert subscription.litellm_key_ids == []
    assert subscription.metadata == {"phase": "s1"}
    assert litellm_client.created_teams == {}
    assert litellm_client.generated_keys == []


@pytest.mark.asyncio
async def test_subscription_version_conflict_returns_409():
    service, _, _, _ = _service()
    created = await service.create_subscription(
        SubscriptionCreateRequest(project_id="project-1", plan_id="plan-basic", issue_key=False)
    )

    with pytest.raises(HTTPException) as exc:
        await service.pause_subscription(
            created.subscription.subscription_id,
            SubscriptionActionRequest(version=created.subscription.version + 1),
        )

    assert exc.value.status_code == 409
    assert exc.value.detail == {"error": "Subscription version conflict", "current_version": created.subscription.version}


@pytest.mark.asyncio
async def test_update_if_version_returns_writer_row_with_json_and_array_casts():
    now = datetime.now(timezone.utc)
    snapshot = plan_snapshot_from_plan(_plan()).model_dump(mode="json")
    row = {
        "subscription_id": "sub-1",
        "project_id": "project-1",
        "plan_id": "plan-basic",
        "status": SubscriptionStatus.ACTIVE.value,
        "plan_snapshot": snapshot,
        "litellm_team_id": "team-1",
        "litellm_key_ids": ["key-1"],
        "expires_at": None,
        "renewed_at": None,
        "paused_at": None,
        "canceled_at": None,
        "metadata": {"project": "one"},
        "version": 2,
        "created_at": now,
        "created_by": None,
        "updated_at": now,
        "updated_by": "admin-user",
    }
    repository = SubscriptionRepository(RawUpdatePrismaClient(row))

    updated = await repository.update_if_version(
        "sub-1",
        1,
        {
            "plan_snapshot": snapshot,
            "litellm_key_ids": ["key-1"],
            "metadata": {"project": "one"},
            "version": 2,
        },
    )

    assert updated is not None
    assert updated.version == 2

    query, args = repository.prisma_client.db.writer.calls[0]
    assert query.startswith('UPDATE "LiteLLM_CodePlanSubscriptionTable" SET ')
    assert '"plan_snapshot" = $1::jsonb' in query
    assert '"litellm_key_ids" = $2::text[]' in query
    assert '"metadata" = $3::jsonb' in query
    assert '"updated_at" = CURRENT_TIMESTAMP' in query
    assert 'WHERE "subscription_id" = $5 AND "version" = $6' in query
    assert args == (json.dumps(snapshot), ["key-1"], '{"project": "one"}', 2, "sub-1", 1)


@pytest.mark.asyncio
async def test_max_keys_and_revoke_key():
    service, _, _, litellm_client = _service([_plan(max_keys=1)])
    created = await service.create_subscription(SubscriptionCreateRequest(project_id="project-1", plan_id="plan-basic"))

    with pytest.raises(HTTPException) as exc:
        await service.issue_key(
            created.subscription.subscription_id,
            SubscriptionIssueKeyRequest(version=created.subscription.version),
        )

    assert exc.value.status_code == 400
    assert "max_keys" in exc.value.detail["error"]

    revoked = await service.revoke_key(
        created.subscription.subscription_id,
        SubscriptionRevokeKeyRequest(version=created.subscription.version, key_id="key-1"),
    )

    assert revoked.litellm_key_ids == []
    assert litellm_client.revoked_keys == ["key-1"]


@pytest.mark.asyncio
async def test_pause_renew_cancel_and_expire_revoke_keys():
    service, _, _, litellm_client = _service()
    created = await service.create_subscription(SubscriptionCreateRequest(project_id="project-1", plan_id="plan-basic"))

    paused = await service.pause_subscription(
        created.subscription.subscription_id,
        SubscriptionActionRequest(version=created.subscription.version),
    )

    assert paused.status == SubscriptionStatus.PAUSED
    assert paused.litellm_key_ids == []
    assert litellm_client.revoked_keys == ["key-1"]

    renewed = await service.renew_subscription(
        paused.subscription_id,
        SubscriptionRenewRequest(version=paused.version, expires_at=datetime.now(timezone.utc) + timedelta(days=7)),
    )

    assert renewed.subscription.status == SubscriptionStatus.ACTIVE
    assert renewed.subscription.renewed_at is not None
    assert renewed.subscription.litellm_key_ids == ["key-2"]

    canceled = await service.cancel_subscription(
        renewed.subscription.subscription_id,
        SubscriptionActionRequest(version=renewed.subscription.version),
    )

    assert canceled.status == SubscriptionStatus.CANCELED
    assert canceled.litellm_key_ids == []
    assert litellm_client.revoked_keys == ["key-1", "key-2"]

    second = await service.create_subscription(SubscriptionCreateRequest(project_id="project-2", plan_id="plan-basic"))
    expired = await service.expire_subscription(
        second.subscription.subscription_id,
        SubscriptionActionRequest(version=second.subscription.version),
    )

    assert expired.status == SubscriptionStatus.EXPIRED
    assert expired.litellm_key_ids == []
    assert litellm_client.revoked_keys[-1] == "key-3"


@pytest.mark.asyncio
async def test_upgrade_rejects_downgrade_and_creates_new_subscription_for_upgrade():
    basic = _plan()
    lower = _plan(plan_id="plan-lower", name="Lower Code", quota_5h=50, quota_weekly=500)
    higher = _plan(
        plan_id="plan-pro",
        name="Pro Code",
        version=2,
        quota_5h=200,
        quota_weekly=2000,
        allowed_models=["anthropic/claude-4-sonnet", "gpt-4.1", "gpt-4.1-mini"],
        max_keys=10,
    )
    service, repository, _, litellm_client = _service([basic, lower, higher])
    created = await service.create_subscription(SubscriptionCreateRequest(project_id="project-1", plan_id="plan-basic"))

    with pytest.raises(HTTPException) as exc:
        await service.upgrade_subscription(
            created.subscription.subscription_id,
            SubscriptionUpgradeRequest(version=created.subscription.version, plan_id="plan-lower"),
        )

    assert exc.value.status_code == 400
    assert "downgrade" in exc.value.detail["error"]

    upgraded = await service.upgrade_subscription(
        created.subscription.subscription_id,
        SubscriptionUpgradeRequest(version=created.subscription.version, plan_id="plan-pro"),
    )

    old = await repository.get(created.subscription.subscription_id)
    assert old is not None
    assert old.status == SubscriptionStatus.CANCELED
    assert old.litellm_key_ids == []
    assert upgraded.subscription.subscription_id != old.subscription_id
    assert upgraded.subscription.plan_id == "plan-pro"
    assert upgraded.subscription.plan_snapshot.quota_5h == 200
    assert upgraded.subscription.litellm_team_id == "team-1"
    assert upgraded.subscription.litellm_key_ids == ["key-2"]
    assert litellm_client.revoked_keys == ["key-1"]
    assert litellm_client.updated_teams[-1][0] == "team-1"
