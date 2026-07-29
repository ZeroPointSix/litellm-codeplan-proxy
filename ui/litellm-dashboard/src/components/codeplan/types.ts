export type PlanStatus = "draft" | "active" | "archived";

export type CreditRuleStatus = "draft" | "active" | "archived";

export type SubscriptionStatus = "active" | "paused" | "canceled" | "expired";

export type UsageLedgerEventType =
  | "reserve"
  | "settle"
  | "release"
  | "refund"
  | "manual_adjust"
  | "compensate"
  | "expire"
  | "pending_usage";

export type UsageLedgerGroupBy = "hour" | "day" | "model" | "user";

export type CodePlanMetadata = Record<string, unknown>;

export interface CodePlanListResponse<T> {
  object: "list";
  data: T[];
}

export interface CodePlanEntitlements {
  quota_5h: number;
  quota_weekly: number;
  allowed_models: string[];
  rpm_limit?: number | null;
  tpm_limit?: number | null;
  max_parallel_requests?: number | null;
  max_keys: number;
  default_max_output_tokens?: number | null;
  credit_rule_id?: string | null;
  metadata: CodePlanMetadata;
}

export interface CodePlanPlan extends CodePlanEntitlements {
  plan_id: string;
  name: string;
  description?: string | null;
  status: PlanStatus;
  version: number;
  created_at?: string | null;
  created_by?: string | null;
  updated_at?: string | null;
  updated_by?: string | null;
}

export interface CodePlanPlanCreateRequest extends CodePlanEntitlements {
  name: string;
  description?: string | null;
}

export interface CodePlanPlanPatchRequest {
  version: number;
  name?: string | null;
  description?: string | null;
  quota_5h?: number | null;
  quota_weekly?: number | null;
  allowed_models?: string[] | null;
  rpm_limit?: number | null;
  tpm_limit?: number | null;
  max_parallel_requests?: number | null;
  max_keys?: number | null;
  default_max_output_tokens?: number | null;
  credit_rule_id?: string | null;
  metadata?: CodePlanMetadata | null;
}

export interface CodePlanCreditRule {
  id: string;
  credit_rule_id: string;
  name: string;
  description?: string | null;
  status: CreditRuleStatus;
  version: number;
  input_multiplier: number;
  output_multiplier: number;
  cache_read_multiplier: number;
  cache_write_multiplier: number;
  effective_at?: string | null;
  metadata: CodePlanMetadata;
  created_at?: string | null;
  created_by?: string | null;
  updated_at?: string | null;
  updated_by?: string | null;
}

export interface CodePlanCreditRuleCreateRequest {
  name: string;
  description?: string | null;
  input_multiplier: number;
  output_multiplier: number;
  cache_read_multiplier: number;
  cache_write_multiplier: number;
  effective_at?: string | null;
  metadata: CodePlanMetadata;
}

export interface CodePlanCreditRulePatchRequest {
  version: number;
  name?: string | null;
  description?: string | null;
  input_multiplier?: number | null;
  output_multiplier?: number | null;
  cache_read_multiplier?: number | null;
  cache_write_multiplier?: number | null;
  effective_at?: string | null;
  metadata?: CodePlanMetadata | null;
}

export interface CodePlanPlanSnapshot extends CodePlanEntitlements {
  plan_id: string;
  name: string;
  description?: string | null;
  plan_version: number;
  credit_rule_version?: number | null;
  credit_input_multiplier: number;
  credit_output_multiplier: number;
  credit_cache_read_multiplier: number;
  credit_cache_write_multiplier: number;
  captured_at: string;
}

export interface CodePlanSubscription {
  subscription_id: string;
  project_id: string;
  plan_id: string;
  status: SubscriptionStatus;
  plan_snapshot: CodePlanPlanSnapshot;
  litellm_team_id?: string | null;
  litellm_key_ids: string[];
  expires_at?: string | null;
  renewed_at?: string | null;
  paused_at?: string | null;
  canceled_at?: string | null;
  window_5h_start?: string | null;
  window_week_start?: string | null;
  metadata: CodePlanMetadata;
  version: number;
  created_at?: string | null;
  created_by?: string | null;
  updated_at?: string | null;
  updated_by?: string | null;
}

export interface CodePlanSubscriptionProvisionResponse {
  subscription: CodePlanSubscription;
  key_id?: string | null;
  key?: string | null;
  token_id?: string | null;
}

export interface CodePlanUsageLedgerEntry {
  event_id: string;
  request_id: string;
  subscription_id: string;
  project_id?: string | null;
  user_id?: string | null;
  api_key_id?: string | null;
  model?: string | null;
  event_type: UsageLedgerEventType;
  credits: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  input_multiplier: number;
  output_multiplier: number;
  cache_read_multiplier: number;
  cache_write_multiplier: number;
  rule_version: number;
  window_5h_period_id?: string | null;
  window_5h_start?: string | null;
  window_5h_end?: string | null;
  window_week_period_id?: string | null;
  window_week_start?: string | null;
  window_week_end?: string | null;
  metadata: CodePlanMetadata;
  created_at?: string | null;
}

export interface CodePlanUsageLedgerAggregate {
  group: string;
  credits: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  request_count: number;
  event_count: number;
}
