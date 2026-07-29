import type { CodePlanMetadata, CodePlanPlan, CodePlanPlanCreateRequest, CodePlanPlanPatchRequest } from "./types";

export interface CodePlanPlanFormValues {
  name: string;
  description?: string | null;
  quota_5h: number;
  quota_weekly: number;
  allowed_models: string[];
  rpm_limit?: number | null;
  tpm_limit?: number | null;
  max_parallel_requests?: number | null;
  max_keys: number;
  default_max_output_tokens?: number | null;
  credit_rule_id?: string | null;
  metadata?: string;
}

export const defaultCodePlanPlanFormValues: CodePlanPlanFormValues = {
  name: "",
  description: null,
  quota_5h: 100,
  quota_weekly: 500,
  allowed_models: [],
  rpm_limit: null,
  tpm_limit: null,
  max_parallel_requests: null,
  max_keys: 1,
  default_max_output_tokens: null,
  credit_rule_id: null,
  metadata: "{}",
};

const optionalNumber = (value?: number | null): number | null => (typeof value === "number" ? value : null);

const textOrNull = (value?: string | null): string | null => {
  const trimmed = value?.trim();
  return trimmed ? trimmed : null;
};

export const parseCodePlanMetadata = (value?: string): CodePlanMetadata => {
  const raw = value?.trim();
  if (!raw) return {};
  const parsed = JSON.parse(raw) as unknown;
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("metadata 必须是 JSON object");
  }
  return parsed as CodePlanMetadata;
};

export const planToFormValues = (plan: CodePlanPlan): CodePlanPlanFormValues => ({
  name: plan.name,
  description: plan.description ?? null,
  quota_5h: plan.quota_5h,
  quota_weekly: plan.quota_weekly,
  allowed_models: plan.allowed_models ?? [],
  rpm_limit: plan.rpm_limit ?? null,
  tpm_limit: plan.tpm_limit ?? null,
  max_parallel_requests: plan.max_parallel_requests ?? null,
  max_keys: plan.max_keys,
  default_max_output_tokens: plan.default_max_output_tokens ?? null,
  credit_rule_id: plan.credit_rule_id ?? null,
  metadata: JSON.stringify(plan.metadata ?? {}, null, 2),
});

export const createPlanPayloadFromValues = (values: CodePlanPlanFormValues): CodePlanPlanCreateRequest => ({
  name: values.name.trim(),
  description: textOrNull(values.description),
  quota_5h: values.quota_5h,
  quota_weekly: values.quota_weekly,
  allowed_models: values.allowed_models ?? [],
  rpm_limit: optionalNumber(values.rpm_limit),
  tpm_limit: optionalNumber(values.tpm_limit),
  max_parallel_requests: optionalNumber(values.max_parallel_requests),
  max_keys: values.max_keys,
  default_max_output_tokens: optionalNumber(values.default_max_output_tokens),
  credit_rule_id: values.credit_rule_id ?? null,
  metadata: parseCodePlanMetadata(values.metadata),
});

export const patchPlanPayloadFromValues = (
  values: CodePlanPlanFormValues,
  version: number,
): CodePlanPlanPatchRequest => ({
  version,
  name: values.name.trim(),
  description: textOrNull(values.description),
  quota_5h: values.quota_5h,
  quota_weekly: values.quota_weekly,
  allowed_models: values.allowed_models ?? [],
  rpm_limit: optionalNumber(values.rpm_limit),
  tpm_limit: optionalNumber(values.tpm_limit),
  max_parallel_requests: optionalNumber(values.max_parallel_requests),
  max_keys: values.max_keys,
  default_max_output_tokens: optionalNumber(values.default_max_output_tokens),
  credit_rule_id: values.credit_rule_id ?? null,
  metadata: parseCodePlanMetadata(values.metadata),
});

export const modelNameFromRecord = (record: { id?: string; model_name?: string }): string | null => {
  const modelName = record.model_name?.trim();
  if (modelName) return modelName;
  const id = record.id?.trim();
  return id || null;
};
