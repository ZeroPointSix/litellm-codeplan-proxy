from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from litellm.product.credit_rules.models import CreditRuleRecord
from litellm.product.plans.litellm_mapping import plan_to_litellm_team_config
from litellm.product.plans.models import PlanRecord
from litellm.product.subscriptions.models import PlanSnapshot


def plan_snapshot_from_plan(plan: PlanRecord, credit_rule: CreditRuleRecord | None = None) -> PlanSnapshot:
    multipliers = _snapshot_multipliers(credit_rule)
    return PlanSnapshot(
        plan_id=plan.plan_id,
        name=plan.name,
        description=plan.description,
        plan_version=plan.version,
        quota_5h=plan.quota_5h,
        quota_weekly=plan.quota_weekly,
        allowed_models=list(plan.allowed_models),
        rpm_limit=plan.rpm_limit,
        tpm_limit=plan.tpm_limit,
        max_parallel_requests=plan.max_parallel_requests,
        max_keys=plan.max_keys,
        default_max_output_tokens=plan.default_max_output_tokens,
        credit_rule_id=plan.credit_rule_id,
        credit_rule_version=getattr(credit_rule, "version", None),
        credit_input_multiplier=multipliers["credit_input_multiplier"],
        credit_output_multiplier=multipliers["credit_output_multiplier"],
        credit_cache_read_multiplier=multipliers["credit_cache_read_multiplier"],
        credit_cache_write_multiplier=multipliers["credit_cache_write_multiplier"],
        metadata=dict(plan.metadata),
        captured_at=datetime.now(timezone.utc),
    )


def snapshot_to_litellm_team_config(snapshot: PlanSnapshot) -> dict[str, Any]:
    plan_like = {
        "plan_id": snapshot.plan_id,
        "name": snapshot.name,
        "description": snapshot.description,
        "version": snapshot.plan_version,
        "quota_5h": snapshot.quota_5h,
        "quota_weekly": snapshot.quota_weekly,
        "allowed_models": list(snapshot.allowed_models),
        "rpm_limit": snapshot.rpm_limit,
        "tpm_limit": snapshot.tpm_limit,
        "max_parallel_requests": snapshot.max_parallel_requests,
        "max_keys": snapshot.max_keys,
        "default_max_output_tokens": snapshot.default_max_output_tokens,
        "credit_rule_id": snapshot.credit_rule_id,
        "metadata": dict(snapshot.metadata),
    }
    config = plan_to_litellm_team_config(plan_like)
    config["metadata"] = dict(config.get("metadata") or {})
    config["metadata"].update(_snapshot_credit_metadata(snapshot))
    config["metadata"]["code_plan_snapshot_captured_at"] = snapshot.captured_at.isoformat()
    config["metadata"]["code_plan_credit_rule_version"] = snapshot.credit_rule_version
    return config


def _snapshot_multipliers(credit_rule: CreditRuleRecord | None) -> dict[str, float]:
    if credit_rule is None:
        return {
            "credit_input_multiplier": 1.0,
            "credit_output_multiplier": 1.0,
            "credit_cache_read_multiplier": 0.0,
            "credit_cache_write_multiplier": 0.0,
        }
    return {
        "credit_input_multiplier": credit_rule.input_multiplier,
        "credit_output_multiplier": credit_rule.output_multiplier,
        "credit_cache_read_multiplier": credit_rule.cache_read_multiplier,
        "credit_cache_write_multiplier": credit_rule.cache_write_multiplier,
    }


def _snapshot_credit_metadata(snapshot: PlanSnapshot) -> dict[str, Any]:
    return {
        "code_plan_quota_5h": snapshot.quota_5h,
        "code_plan_quota_weekly": snapshot.quota_weekly,
        "code_plan_default_max_output_tokens": snapshot.default_max_output_tokens,
        "code_plan_credit_input_multiplier": snapshot.credit_input_multiplier,
        "code_plan_credit_output_multiplier": snapshot.credit_output_multiplier,
        "code_plan_credit_cache_read_multiplier": snapshot.credit_cache_read_multiplier,
        "code_plan_credit_cache_write_multiplier": snapshot.credit_cache_write_multiplier,
    }
