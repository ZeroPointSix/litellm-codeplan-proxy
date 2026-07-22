from typing import Any, Mapping, Union

from litellm.product.plans.models import PlanRecord


def _drop_none(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _drop_none(child) for key, child in value.items() if child is not None}
    if isinstance(value, list):
        return [_drop_none(child) for child in value]
    return value


def plan_to_litellm_team_config(plan: Union[PlanRecord, Mapping[str, Any]]) -> dict[str, Any]:
    """Convert a Plan into LiteLLM team config without calling LiteLLM."""
    plan_record = plan if isinstance(plan, PlanRecord) else PlanRecord.model_validate(dict(plan))

    return _drop_none(
        {
            "team_alias": plan_record.name,
            "models": list(plan_record.allowed_models),
            "max_budget": float(plan_record.quota_weekly),
            "budget_limits": [
                {"budget_duration": "5h", "max_budget": float(plan_record.quota_5h)},
                {"budget_duration": "7d", "max_budget": float(plan_record.quota_weekly)},
            ],
            "rpm_limit": plan_record.rpm_limit,
            "tpm_limit": plan_record.tpm_limit,
            "max_parallel_requests": plan_record.max_parallel_requests,
            "metadata": {
                **plan_record.metadata,
                "code_plan_id": plan_record.plan_id,
                "code_plan_name": plan_record.name,
                "code_plan_version": plan_record.version,
                "code_plan_max_keys": plan_record.max_keys,
                "code_plan_default_max_output_tokens": plan_record.default_max_output_tokens,
                "code_plan_credit_rule_id": plan_record.credit_rule_id,
            },
        }
    )
