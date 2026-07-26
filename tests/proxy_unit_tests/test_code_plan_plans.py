from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from litellm.product.plans.litellm_mapping import plan_to_litellm_team_config
from litellm.product.plans.models import PlanCreateRequest, PlanPatchRequest, PlanRecord, PlanStatus
from litellm.product.plans.repository import PlanRepository
from litellm.product.plans.router import CODE_PLAN_MANAGEMENT_ROUTES
from litellm.product.plans.service import PlanService
from litellm.proxy._types import LiteLLMRoutes
from litellm.proxy.auth.route_checks import RouteChecks


class InMemoryPlanRepository:
    def __init__(self):
        self.records: dict[str, PlanRecord] = {}
        self.counter = 0

    async def create(self, data):
        self.counter += 1
        now = datetime.now(timezone.utc)
        record = PlanRecord(
            plan_id=f"plan-{self.counter}",
            created_at=now,
            updated_at=now,
            **data,
        )
        self.records[record.plan_id] = record
        return record

    async def get(self, plan_id):
        return self.records.get(plan_id)

    async def list(self, status=None):
        records = list(self.records.values())
        if status:
            records = [record for record in records if record.status == status]
        return records

    async def update(self, plan_id, data):
        existing = self.records[plan_id]
        updated = existing.model_copy(update=data)
        self.records[plan_id] = updated
        return updated

    async def update_if_version(self, plan_id, version, data):
        existing = self.records[plan_id]
        if existing.version != version:
            return None
        return await self.update(plan_id, data)


class RacingPlanRepository(InMemoryPlanRepository):
    async def update_if_version(self, plan_id, version, data):
        existing = self.records[plan_id]
        self.records[plan_id] = existing.model_copy(update={"version": existing.version + 1})
        return None


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
        self.litellm_codeplantable = ReplicaReadActions()


class RawUpdatePrismaClient:
    def __init__(self, row):
        self.db = RawUpdateDB(row)


def _create_request(**overrides):
    data = {
        "name": "Pro Code",
        "quota_5h": 100,
        "quota_weekly": 1000,
        "allowed_models": ["anthropic/claude-4-sonnet", "gpt-4.1"],
    }
    data.update(overrides)
    return PlanCreateRequest(**data)


def test_plan_to_litellm_team_config_maps_entitlements():
    plan = PlanRecord(
        plan_id="plan-1",
        name="Pro Code",
        status=PlanStatus.ACTIVE,
        version=3,
        quota_5h=100,
        quota_weekly=1000,
        allowed_models=["anthropic/claude-4-sonnet", "gpt-4.1"],
        rpm_limit=60,
        tpm_limit=120000,
        max_parallel_requests=4,
        max_keys=7,
        default_max_output_tokens=4096,
        credit_rule_id="rule-1",
        metadata={"tier": "pro", "quota_monthly": 4000},
    )

    config = plan_to_litellm_team_config(plan)

    assert config["team_alias"] == "Pro Code"
    assert config["models"] == ["anthropic/claude-4-sonnet", "gpt-4.1"]
    assert "max_budget" not in config
    assert "budget_limits" not in config
    assert config["rpm_limit"] == 60
    assert config["tpm_limit"] == 120000
    assert config["max_parallel_requests"] == 4
    assert config["metadata"]["code_plan_id"] == "plan-1"
    assert config["metadata"]["code_plan_version"] == 3
    assert config["metadata"]["code_plan_quota_5h"] == 100
    assert config["metadata"]["code_plan_quota_weekly"] == 1000
    assert config["metadata"]["code_plan_quota_monthly"] == 4000
    assert config["metadata"]["code_plan_max_keys"] == 7
    assert config["metadata"]["code_plan_default_max_output_tokens"] == 4096
    assert config["metadata"]["code_plan_credit_rule_id"] == "rule-1"
    assert config["metadata"]["code_plan_native_budget_disabled"] is True


def test_code_plan_routes_are_management_routes():
    management_routes = LiteLLMRoutes.management_routes.value
    for route in CODE_PLAN_MANAGEMENT_ROUTES:
        assert route in management_routes

    assert RouteChecks.check_route_access(route="/v1/admin/plans", allowed_routes=management_routes)
    assert RouteChecks.check_route_access(route="/v1/admin/plans/plan-1", allowed_routes=management_routes)
    assert RouteChecks.check_route_access(route="/v1/admin/plans/plan-1/activate", allowed_routes=management_routes)
    assert RouteChecks.check_route_access(route="/v1/admin/plans/plan-1/archive", allowed_routes=management_routes)


@pytest.mark.asyncio
async def test_update_if_version_returns_writer_row_without_replica_read():
    now = datetime.now(timezone.utc)
    row = {
        "plan_id": "plan-1",
        "name": "Pro Code",
        "description": None,
        "status": PlanStatus.DRAFT.value,
        "version": 2,
        "quota_5h": 200,
        "quota_weekly": 1000,
        "allowed_models": ["anthropic/claude-4-sonnet"],
        "rpm_limit": None,
        "tpm_limit": None,
        "max_parallel_requests": None,
        "max_keys": 5,
        "default_max_output_tokens": None,
        "credit_rule_id": None,
        "metadata": {"tier": "pro"},
        "created_at": now,
        "created_by": None,
        "updated_at": now,
        "updated_by": "admin-user",
    }
    repository = PlanRepository(RawUpdatePrismaClient(row))

    updated = await repository.update_if_version(
        "plan-1",
        1,
        {"quota_5h": 200, "version": 2, "metadata": {"tier": "pro"}},
    )

    assert updated is not None
    assert updated.version == 2
    assert updated.quota_5h == 200
    assert updated.metadata == {"tier": "pro"}

    query, args = repository.prisma_client.db.writer.calls[0]
    assert query.startswith('UPDATE "LiteLLM_CodePlanTable" SET ')
    assert '"quota_5h" = $1' in query
    assert '"version" = $2' in query
    assert '"metadata" = $3::jsonb' in query
    assert '"updated_at" = CURRENT_TIMESTAMP' in query
    assert 'WHERE "plan_id" = $4 AND "version" = $5' in query
    assert args == (200, 2, '{"tier": "pro"}', "plan-1", 1)


@pytest.mark.asyncio
async def test_draft_to_active_to_archived_flow():
    service = PlanService(InMemoryPlanRepository())

    draft = await service.create_plan(_create_request())
    assert draft.status == PlanStatus.DRAFT
    assert draft.version == 1

    active = await service.activate_plan(draft.plan_id)
    assert active.status == PlanStatus.ACTIVE
    assert active.version == 2

    archived = await service.archive_plan(active.plan_id)
    assert archived.status == PlanStatus.ARCHIVED
    assert archived.version == 3


@pytest.mark.asyncio
async def test_activate_requires_allowed_models():
    service = PlanService(InMemoryPlanRepository())
    draft = await service.create_plan(_create_request(allowed_models=[]))

    with pytest.raises(HTTPException) as exc:
        await service.activate_plan(draft.plan_id, version=draft.version)

    assert exc.value.status_code == 400
    assert "allowed_models" in exc.value.detail["error"]


@pytest.mark.asyncio
async def test_patch_requires_current_version_and_preserves_weekly_window():
    service = PlanService(InMemoryPlanRepository())
    draft = await service.create_plan(_create_request())

    with pytest.raises(HTTPException) as exc:
        await service.patch_plan(draft.plan_id, PlanPatchRequest(version=999, quota_5h=200))
    assert exc.value.status_code == 409

    with pytest.raises(HTTPException) as exc:
        await service.patch_plan(draft.plan_id, PlanPatchRequest(version=draft.version, quota_5h=2000))
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_patch_returns_409_when_atomic_update_misses_version():
    repository = RacingPlanRepository()
    service = PlanService(repository)
    draft = await service.create_plan(_create_request())

    with pytest.raises(HTTPException) as exc:
        await service.patch_plan(draft.plan_id, PlanPatchRequest(version=draft.version, quota_5h=200))

    assert exc.value.status_code == 409
    assert exc.value.detail["current_version"] == 2
    assert repository.records[draft.plan_id].quota_5h == draft.quota_5h


@pytest.mark.asyncio
async def test_archived_plan_cannot_be_modified():
    service = PlanService(InMemoryPlanRepository())
    draft = await service.create_plan(_create_request())
    active = await service.activate_plan(draft.plan_id, version=draft.version)
    archived = await service.archive_plan(active.plan_id, version=active.version)

    with pytest.raises(HTTPException) as exc:
        await service.patch_plan(archived.plan_id, PlanPatchRequest(version=archived.version, quota_5h=200))

    assert exc.value.status_code == 400
