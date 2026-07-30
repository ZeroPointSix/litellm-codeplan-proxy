import { getProxyBaseUrl } from "@/components/networking";
import { ApiError, createApiClient, deriveErrorMessage, type QueryParams } from "@/lib/http/client";
import type {
  CodePlanCreditRule,
  CodePlanCreditRuleCreateRequest,
  CodePlanCreditRulePatchRequest,
  CodePlanListResponse,
  CodePlanPlan,
  CodePlanPlanCreateRequest,
  CodePlanPlanPatchRequest,
  CodePlanSubscription,
  CodePlanSubscriptionProvisionResponse,
  CodePlanUsageLedgerAggregate,
  CodePlanUsageLedgerEntry,
  CreditRuleStatus,
  PlanStatus,
  SubscriptionStatus,
  UsageLedgerEventType,
  UsageLedgerGroupBy,
} from "./types";

export const CODEPLAN_ADMIN_ENDPOINTS = {
  plans: "/v1/admin/plans",
  creditRules: "/v1/admin/credit-rules",
  subscriptions: "/v1/admin/subscriptions",
  usageLedger: "/v1/admin/usage-ledger",
  usageLedgerSummary: "/v1/admin/usage-ledger/summary",
  usageLedgerManualAdjust: "/v1/admin/usage-ledger/manual-adjust",
  models: "/v1/models",
} as const;

type AccessToken = string | null | undefined;
type StatusQuery<TStatus extends string> = QueryParams & { status?: TStatus | null };

export type CodePlanPlanListQuery = StatusQuery<PlanStatus>;

export type CodePlanCreditRuleListQuery = StatusQuery<CreditRuleStatus> & {
  credit_rule_id?: string | null;
};

export type CodePlanSubscriptionListQuery = StatusQuery<SubscriptionStatus> & {
  project_id?: string | null;
  plan_id?: string | null;
};

export type CodePlanUsageLedgerQuery = QueryParams & {
  request_id?: string | null;
  subscription_id?: string | null;
  project_id?: string | null;
  user_id?: string | null;
  model?: string | null;
  event_type?: UsageLedgerEventType | null;
  start_time?: string | null;
  end_time?: string | null;
  limit?: number | null;
};

export type CodePlanUsageLedgerSummaryQuery = CodePlanUsageLedgerQuery & {
  group_by?: UsageLedgerGroupBy | null;
};

export interface CodePlanAvailableModel {
  id?: string;
  model_name?: string;
  owned_by?: string;
  object?: string;
}

export interface CodePlanAvailableModelListResponse {
  object?: string;
  data?: CodePlanAvailableModel[];
}

export interface CodePlanSubscriptionCreateRequest {
  project_id: string;
  plan_id: string;
  expires_at?: string | null;
  issue_key?: boolean;
  key_alias?: string | null;
  metadata?: Record<string, unknown>;
}

export interface CodePlanSubscriptionActionRequest {
  version: number;
}

export interface CodePlanSubscriptionRenewRequest extends CodePlanSubscriptionActionRequest {
  expires_at?: string | null;
  issue_key?: boolean;
  key_alias?: string | null;
  metadata?: Record<string, unknown>;
}

export interface CodePlanSubscriptionUpgradeRequest extends CodePlanSubscriptionRenewRequest {
  plan_id: string;
}

export interface CodePlanSubscriptionIssueKeyRequest extends CodePlanSubscriptionActionRequest {
  key_alias?: string | null;
  metadata?: Record<string, unknown>;
}

export interface CodePlanSubscriptionRevokeKeyRequest extends CodePlanSubscriptionActionRequest {
  key_id: string;
}

export interface CodePlanUsageLedgerManualAdjustRequest {
  subscription_id: string;
  request_id: string;
  credits: number;
  reason: string;
  project_id?: string | null;
  user_id?: string | null;
  model?: string | null;
  rule_version?: number;
  metadata?: Record<string, unknown>;
}

const codePlanClient = createApiClient({ getBaseUrl: getProxyBaseUrl });

const itemPath = (basePath: string, id: string): string => `${basePath}/${encodeURIComponent(id)}`;

const versionQuery = (version?: number): QueryParams | undefined => (version === undefined ? undefined : { version });

export const isCodePlanVersionConflict = (error: unknown): boolean => error instanceof ApiError && error.status === 409;

export const formatCodePlanError = (error: unknown): string => {
  if (error instanceof ApiError) {
    if (error.status === 403) return "需要 Proxy Admin 权限";
    if (error.status === 409) return "版本已变化，请刷新后重试";
    if (error.status === 500) return "数据库未连接";
    return error.message || deriveErrorMessage(error.body);
  }
  if (error instanceof Error) return error.message;
  return "请求失败";
};

export const listCodePlanPlans = (accessToken: AccessToken, query?: CodePlanPlanListQuery) =>
  codePlanClient.get<CodePlanListResponse<CodePlanPlan>>(CODEPLAN_ADMIN_ENDPOINTS.plans, { accessToken, query });

export const createCodePlanPlan = (accessToken: AccessToken, body: CodePlanPlanCreateRequest) =>
  codePlanClient.post<CodePlanPlan>(CODEPLAN_ADMIN_ENDPOINTS.plans, { accessToken, body });

export const getCodePlanPlan = (accessToken: AccessToken, planId: string) =>
  codePlanClient.get<CodePlanPlan>(itemPath(CODEPLAN_ADMIN_ENDPOINTS.plans, planId), { accessToken });

export const updateCodePlanPlan = (accessToken: AccessToken, planId: string, body: CodePlanPlanPatchRequest) =>
  codePlanClient.patch<CodePlanPlan>(itemPath(CODEPLAN_ADMIN_ENDPOINTS.plans, planId), { accessToken, body });

export const activateCodePlanPlan = (accessToken: AccessToken, planId: string, version?: number) =>
  codePlanClient.post<CodePlanPlan>(`${itemPath(CODEPLAN_ADMIN_ENDPOINTS.plans, planId)}/activate`, {
    accessToken,
    query: versionQuery(version),
  });

export const archiveCodePlanPlan = (accessToken: AccessToken, planId: string, version?: number) =>
  codePlanClient.post<CodePlanPlan>(`${itemPath(CODEPLAN_ADMIN_ENDPOINTS.plans, planId)}/archive`, {
    accessToken,
    query: versionQuery(version),
  });

export const listCodePlanCreditRules = (accessToken: AccessToken, query?: CodePlanCreditRuleListQuery) =>
  codePlanClient.get<CodePlanListResponse<CodePlanCreditRule>>(CODEPLAN_ADMIN_ENDPOINTS.creditRules, {
    accessToken,
    query,
  });

export const createCodePlanCreditRule = (accessToken: AccessToken, body: CodePlanCreditRuleCreateRequest) =>
  codePlanClient.post<CodePlanCreditRule>(CODEPLAN_ADMIN_ENDPOINTS.creditRules, { accessToken, body });

export const getCodePlanCreditRule = (accessToken: AccessToken, creditRuleId: string, version?: number) =>
  codePlanClient.get<CodePlanCreditRule>(itemPath(CODEPLAN_ADMIN_ENDPOINTS.creditRules, creditRuleId), {
    accessToken,
    query: versionQuery(version),
  });

export const updateCodePlanCreditRule = (
  accessToken: AccessToken,
  creditRuleId: string,
  body: CodePlanCreditRulePatchRequest,
) =>
  codePlanClient.patch<CodePlanCreditRule>(itemPath(CODEPLAN_ADMIN_ENDPOINTS.creditRules, creditRuleId), {
    accessToken,
    body,
  });

export const activateCodePlanCreditRule = (accessToken: AccessToken, creditRuleId: string, version?: number) =>
  codePlanClient.post<CodePlanCreditRule>(`${itemPath(CODEPLAN_ADMIN_ENDPOINTS.creditRules, creditRuleId)}/activate`, {
    accessToken,
    query: versionQuery(version),
  });

export const archiveCodePlanCreditRule = (accessToken: AccessToken, creditRuleId: string, version?: number) =>
  codePlanClient.post<CodePlanCreditRule>(`${itemPath(CODEPLAN_ADMIN_ENDPOINTS.creditRules, creditRuleId)}/archive`, {
    accessToken,
    query: versionQuery(version),
  });

export const listCodePlanSubscriptions = (accessToken: AccessToken, query?: CodePlanSubscriptionListQuery) =>
  codePlanClient.get<CodePlanListResponse<CodePlanSubscription>>(CODEPLAN_ADMIN_ENDPOINTS.subscriptions, {
    accessToken,
    query,
  });

export const createCodePlanSubscription = (accessToken: AccessToken, body: CodePlanSubscriptionCreateRequest) =>
  codePlanClient.post<CodePlanSubscriptionProvisionResponse>(CODEPLAN_ADMIN_ENDPOINTS.subscriptions, {
    accessToken,
    body,
  });

export const getCodePlanSubscription = (accessToken: AccessToken, subscriptionId: string) =>
  codePlanClient.get<CodePlanSubscription>(itemPath(CODEPLAN_ADMIN_ENDPOINTS.subscriptions, subscriptionId), {
    accessToken,
  });

export const renewCodePlanSubscription = (
  accessToken: AccessToken,
  subscriptionId: string,
  body: CodePlanSubscriptionRenewRequest,
) =>
  codePlanClient.post<CodePlanSubscriptionProvisionResponse>(
    `${itemPath(CODEPLAN_ADMIN_ENDPOINTS.subscriptions, subscriptionId)}/renew`,
    { accessToken, body },
  );

const postSubscriptionAction = (
  accessToken: AccessToken,
  subscriptionId: string,
  action: string,
  body: CodePlanSubscriptionActionRequest,
) =>
  codePlanClient.post<CodePlanSubscription>(
    `${itemPath(CODEPLAN_ADMIN_ENDPOINTS.subscriptions, subscriptionId)}/${action}`,
    {
      accessToken,
      body,
    },
  );

export const pauseCodePlanSubscription = (
  accessToken: AccessToken,
  subscriptionId: string,
  body: CodePlanSubscriptionActionRequest,
) => postSubscriptionAction(accessToken, subscriptionId, "pause", body);

export const cancelCodePlanSubscription = (
  accessToken: AccessToken,
  subscriptionId: string,
  body: CodePlanSubscriptionActionRequest,
) => postSubscriptionAction(accessToken, subscriptionId, "cancel", body);

export const expireCodePlanSubscription = (
  accessToken: AccessToken,
  subscriptionId: string,
  body: CodePlanSubscriptionActionRequest,
) => postSubscriptionAction(accessToken, subscriptionId, "expire", body);

export const upgradeCodePlanSubscription = (
  accessToken: AccessToken,
  subscriptionId: string,
  body: CodePlanSubscriptionUpgradeRequest,
) =>
  codePlanClient.post<CodePlanSubscriptionProvisionResponse>(
    `${itemPath(CODEPLAN_ADMIN_ENDPOINTS.subscriptions, subscriptionId)}/upgrade`,
    { accessToken, body },
  );

export const issueCodePlanSubscriptionKey = (
  accessToken: AccessToken,
  subscriptionId: string,
  body: CodePlanSubscriptionIssueKeyRequest,
) =>
  codePlanClient.post<CodePlanSubscriptionProvisionResponse>(
    `${itemPath(CODEPLAN_ADMIN_ENDPOINTS.subscriptions, subscriptionId)}/keys`,
    { accessToken, body },
  );

export const revokeCodePlanSubscriptionKey = (
  accessToken: AccessToken,
  subscriptionId: string,
  body: CodePlanSubscriptionRevokeKeyRequest,
) =>
  codePlanClient.post<CodePlanSubscription>(
    `${itemPath(CODEPLAN_ADMIN_ENDPOINTS.subscriptions, subscriptionId)}/keys/revoke`,
    {
      accessToken,
      body,
    },
  );

export const listCodePlanUsageLedger = (accessToken: AccessToken, query?: CodePlanUsageLedgerQuery) =>
  codePlanClient.get<CodePlanListResponse<CodePlanUsageLedgerEntry>>(CODEPLAN_ADMIN_ENDPOINTS.usageLedger, {
    accessToken,
    query,
  });

export const summarizeCodePlanUsageLedger = (accessToken: AccessToken, query?: CodePlanUsageLedgerSummaryQuery) =>
  codePlanClient.get<CodePlanListResponse<CodePlanUsageLedgerAggregate> & { group_by: UsageLedgerGroupBy }>(
    CODEPLAN_ADMIN_ENDPOINTS.usageLedgerSummary,
    { accessToken, query },
  );

export const manualAdjustCodePlanUsageLedger = (
  accessToken: AccessToken,
  body: CodePlanUsageLedgerManualAdjustRequest,
) =>
  codePlanClient.post<CodePlanListResponse<CodePlanUsageLedgerEntry>>(
    CODEPLAN_ADMIN_ENDPOINTS.usageLedgerManualAdjust,
    {
      accessToken,
      body,
    },
  );

export const listCodePlanAvailableModels = (accessToken: AccessToken) =>
  codePlanClient.get<CodePlanAvailableModelListResponse>(CODEPLAN_ADMIN_ENDPOINTS.models, { accessToken });
