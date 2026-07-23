from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from litellm.product.credit_rules.models import (
    CreditRuleCreateRequest,
    CreditRulePatchRequest,
    CreditRuleRecord,
    CreditRuleStatus,
)
from litellm.product.credit_rules.router import CREDIT_RULE_MANAGEMENT_ROUTES
from litellm.product.credit_rules.service import CreditRuleService
from litellm.product.plans.models import PlanCreateRequest, PlanStatus
from litellm.product.plans.service import PlanService
from litellm.proxy._types import LiteLLMRoutes
from litellm.proxy.auth.route_checks import RouteChecks
from tests.proxy_unit_tests.test_code_plan_plans import InMemoryPlanRepository


class InMemoryCreditRuleRepository:
    def __init__(self):
        self.records: list[CreditRuleRecord] = []
        self.counter = 0

    async def create(self, data):
        self.counter += 1
        now = datetime.now(timezone.utc)
        payload = dict(data)
        payload.setdefault("credit_rule_id", f"rule-{self.counter}")
        record = CreditRuleRecord(
            id=f"credit-rule-row-{self.counter}",
            created_at=now,
            updated_at=now,
            **payload,
        )
        self.records.append(record)
        return record

    async def get(self, credit_rule_id, version=None):
        matches = [record for record in self.records if record.credit_rule_id == credit_rule_id]
        if version is not None:
            return next((record for record in matches if record.version == version), None)
        return max(matches, key=lambda record: record.version, default=None)

    async def get_active(self, credit_rule_id):
        matches = [
            record
            for record in self.records
            if record.credit_rule_id == credit_rule_id and record.status == CreditRuleStatus.ACTIVE
        ]
        return max(matches, key=lambda record: record.version, default=None)

    async def list(self, status=None, credit_rule_id=None):
        records = self.records
        if status is not None:
            records = [record for record in records if record.status == status]
        if credit_rule_id is not None:
            records = [record for record in records if record.credit_rule_id == credit_rule_id]
        return records

    async def archive_active_versions(self, credit_rule_id, actor_id=None):
        updated = 0
        new_records = []
        for record in self.records:
            if record.credit_rule_id == credit_rule_id and record.status == CreditRuleStatus.ACTIVE:
                new_records.append(record.model_copy(update={"status": CreditRuleStatus.ARCHIVED, "updated_by": actor_id}))
                updated += 1
            else:
                new_records.append(record)
        self.records = new_records
        return updated

    async def update_status(self, credit_rule_id, version, status, actor_id=None):
        new_records = []
        updated = None
        for record in self.records:
            if record.credit_rule_id == credit_rule_id and record.version == version:
                updated = record.model_copy(update={"status": CreditRuleStatus(status), "updated_by": actor_id})
                new_records.append(updated)
            else:
                new_records.append(record)
        self.records = new_records
        return updated


def _rule_request(**overrides):
    data = {
        "name": "Default credits",
        "input_multiplier": 1.0,
        "output_multiplier": 4.0,
        "cache_read_multiplier": 0.1,
        "cache_write_multiplier": 1.25,
    }
    data.update(overrides)
    return CreditRuleCreateRequest(**data)


def _plan_request(**overrides):
    data = {
        "name": "Pro Code",
        "quota_5h": 100,
        "quota_weekly": 1000,
        "allowed_models": ["anthropic/claude-4-sonnet", "gpt-4.1"],
    }
    data.update(overrides)
    return PlanCreateRequest(**data)


def test_credit_rule_routes_are_management_routes():
    management_routes = LiteLLMRoutes.management_routes.value
    for route in CREDIT_RULE_MANAGEMENT_ROUTES:
        assert route in management_routes

    assert RouteChecks.check_route_access(route="/v1/admin/credit-rules", allowed_routes=management_routes)
    assert RouteChecks.check_route_access(route="/v1/admin/credit-rules/rule-1", allowed_routes=management_routes)
    assert RouteChecks.check_route_access(route="/v1/admin/credit-rules/rule-1/activate", allowed_routes=management_routes)
    assert RouteChecks.check_route_access(route="/v1/admin/credit-rules/rule-1/archive", allowed_routes=management_routes)


@pytest.mark.asyncio
async def test_credit_rule_create_patch_keeps_version_history():
    repository = InMemoryCreditRuleRepository()
    service = CreditRuleService(repository)

    draft = await service.create_rule(_rule_request(metadata={"scope": "default"}))
    assert draft.status == CreditRuleStatus.DRAFT
    assert draft.version == 1

    second = await service.patch_rule(
        draft.credit_rule_id,
        CreditRulePatchRequest(version=draft.version, output_multiplier=5.0),
    )
    assert second.credit_rule_id == draft.credit_rule_id
    assert second.version == 2
    assert second.status == CreditRuleStatus.DRAFT
    assert second.output_multiplier == 5.0

    first = await service.get_rule(draft.credit_rule_id, version=1)
    history = await service.list_rules(credit_rule_id=draft.credit_rule_id)
    assert first.output_multiplier == 4.0
    assert {record.version for record in history} == {1, 2}


@pytest.mark.asyncio
async def test_credit_rule_activation_archives_previous_active_version():
    repository = InMemoryCreditRuleRepository()
    service = CreditRuleService(repository)
    draft = await service.create_rule(_rule_request())
    active = await service.activate_rule(draft.credit_rule_id, version=draft.version)
    second = await service.patch_rule(
        active.credit_rule_id,
        CreditRulePatchRequest(version=active.version, cache_read_multiplier=0.05),
    )

    next_active = await service.activate_rule(second.credit_rule_id, version=second.version)

    first = await service.get_rule(active.credit_rule_id, version=1)
    assert first.status == CreditRuleStatus.ARCHIVED
    assert next_active.status == CreditRuleStatus.ACTIVE
    assert next_active.version == 2


@pytest.mark.asyncio
async def test_credit_rule_patch_requires_latest_version_and_non_archived_rule():
    repository = InMemoryCreditRuleRepository()
    service = CreditRuleService(repository)
    draft = await service.create_rule(_rule_request())
    second = await service.patch_rule(draft.credit_rule_id, CreditRulePatchRequest(version=1, input_multiplier=1.5))

    with pytest.raises(HTTPException) as exc:
        await service.patch_rule(draft.credit_rule_id, CreditRulePatchRequest(version=1, input_multiplier=2.0))
    assert exc.value.status_code == 409
    assert exc.value.detail["current_version"] == second.version

    archived = await service.archive_rule(draft.credit_rule_id, version=second.version)
    with pytest.raises(HTTPException) as exc:
        await service.patch_rule(archived.credit_rule_id, CreditRulePatchRequest(version=archived.version, input_multiplier=2.0))
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_plan_binding_requires_active_credit_rule():
    credit_rules = InMemoryCreditRuleRepository()
    plan_service = PlanService(InMemoryPlanRepository(), credit_rule_repository=credit_rules)

    draft_rule = await CreditRuleService(credit_rules).create_rule(_rule_request())
    with pytest.raises(HTTPException) as exc:
        await plan_service.create_plan(_plan_request(credit_rule_id=draft_rule.credit_rule_id))
    assert exc.value.status_code == 400

    active_rule = await CreditRuleService(credit_rules).activate_rule(draft_rule.credit_rule_id, version=draft_rule.version)
    draft_plan = await plan_service.create_plan(_plan_request(credit_rule_id=active_rule.credit_rule_id))
    active_plan = await plan_service.activate_plan(draft_plan.plan_id, version=draft_plan.version)
    assert active_plan.status == PlanStatus.ACTIVE
    assert active_plan.credit_rule_id == active_rule.credit_rule_id


@pytest.mark.asyncio
async def test_plan_activation_rechecks_credit_rule_status():
    credit_rules = InMemoryCreditRuleRepository()
    rule_service = CreditRuleService(credit_rules)
    draft_rule = await rule_service.create_rule(_rule_request())
    active_rule = await rule_service.activate_rule(draft_rule.credit_rule_id, version=draft_rule.version)
    plan_service = PlanService(InMemoryPlanRepository(), credit_rule_repository=credit_rules)
    draft_plan = await plan_service.create_plan(_plan_request(credit_rule_id=active_rule.credit_rule_id))
    await rule_service.archive_rule(active_rule.credit_rule_id, version=active_rule.version)

    with pytest.raises(HTTPException) as exc:
        await plan_service.activate_plan(draft_plan.plan_id, version=draft_plan.version)
    assert exc.value.status_code == 400
