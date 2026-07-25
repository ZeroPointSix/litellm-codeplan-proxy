from typing import Any, Mapping, Union

from litellm.product.plans.models import PlanRecord

CODE_PLAN_CREDIT_BUDGET_DURATION = "7d"


def code_plan_credit_budget_limits(quota_weekly: int) -> list[dict[str, str | float]]:
    """Map Code Plan credits 1:1 onto a single LiteLLM budget window."""
    return [
        {"budget_duration": CODE_PLAN_CREDIT_BUDGET_DURATION, "max_budget": float(quota_weekly)},
    ]


def _drop_none(value: Any) -> Any:
    if not isinstance(value, (dict, list)):
        return value

    root: Any = {} if isinstance(value, dict) else []
    stack: list[tuple[Any, Any]] = [(value, root)]

    while stack:
        source, target = stack.pop()
        if isinstance(source, dict):
            for key, child in source.items():
                if child is None:
                    continue
                if isinstance(child, dict):
                    cleaned_child: Any = {}
                    target[key] = cleaned_child
                    stack.append((child, cleaned_child))
                elif isinstance(child, list):
                    cleaned_child = []
                    target[key] = cleaned_child
                    stack.append((child, cleaned_child))
                else:
                    target[key] = child
            continue

        for child in source:
            if isinstance(child, dict):
                cleaned_child = {}
                target.append(cleaned_child)
                stack.append((child, cleaned_child))
            elif isinstance(child, list):
                cleaned_child = []
                target.append(cleaned_child)
                stack.append((child, cleaned_child))
            else:
                target.append(child)

    return root


def plan_to_litellm_team_config(plan: Union[PlanRecord, Mapping[str, Any]]) -> dict[str, Any]:
    """Convert a Plan into LiteLLM team config without calling LiteLLM."""
    plan_record = plan if isinstance(plan, PlanRecord) else PlanRecord.model_validate(dict(plan))

    return _drop_none(
        {
            "team_alias": plan_record.name,
            "models": list(plan_record.allowed_models),
            "max_budget": float(plan_record.quota_weekly),
            "budget_limits": code_plan_credit_budget_limits(plan_record.quota_weekly),
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
