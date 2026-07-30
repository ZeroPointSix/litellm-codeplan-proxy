"use client";

import type { TableProps } from "antd";
import {
  Alert,
  Button,
  Card,
  DatePicker,
  Descriptions,
  Drawer,
  Form,
  Input,
  Modal,
  Progress,
  Segmented,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from "antd";
import {
  CheckCircleOutlined,
  CloseCircleOutlined,
  DeleteOutlined,
  EyeInvisibleOutlined,
  KeyOutlined,
  PauseCircleOutlined,
  PlusOutlined,
  ReloadOutlined,
  SyncOutlined,
} from "@ant-design/icons";
import dayjs, { type Dayjs } from "dayjs";
import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { isProxyAdminRole } from "@/utils/roles";
import {
  cancelCodePlanSubscription,
  createCodePlanSubscription,
  expireCodePlanSubscription,
  formatCodePlanError,
  getCodePlanSubscription,
  isCodePlanVersionConflict,
  issueCodePlanSubscriptionKey,
  listCodePlanPlans,
  listCodePlanSubscriptions,
  listCodePlanUsageLedger,
  pauseCodePlanSubscription,
  renewCodePlanSubscription,
  revokeCodePlanSubscriptionKey,
  upgradeCodePlanSubscription,
} from "./codeplan_networking";
import { CODE_PLAN_PAGE_DEFINITIONS, CodePlanBreadcrumb, CodePlanEmptyState, CodePlanErrorState } from "./CodePlanPage";
import type {
  CodePlanMetadata,
  CodePlanPlan,
  CodePlanPlanSnapshot,
  CodePlanSubscription,
  CodePlanSubscriptionProvisionResponse,
  CodePlanUsageLedgerEntry,
  SubscriptionStatus,
} from "./types";

const { Paragraph, Text, Title } = Typography;

interface CodePlanSubscriptionsPageProps {
  accessToken?: string | null;
  userRole?: string | null;
}

type StatusFilter = SubscriptionStatus | "all";
type SimpleSubscriptionAction = "pause" | "cancel" | "expire";

interface CreateSubscriptionFormValues {
  project_id?: string;
  plan_id?: string;
  expires_at?: Dayjs | null;
  issue_key?: boolean;
  key_alias?: string;
  metadata?: string;
}

interface RenewSubscriptionFormValues {
  expires_at?: Dayjs | null;
  issue_key?: boolean;
  key_alias?: string;
  metadata?: string;
}

interface UpgradeSubscriptionFormValues extends RenewSubscriptionFormValues {
  plan_id?: string;
}

interface IssueKeyFormValues {
  key_alias?: string;
  metadata?: string;
}

interface RevokeKeyFormValues {
  key_id?: string;
}

interface UsageEstimate {
  fiveHourUsed: number | null;
  weekUsed: number | null;
  eventCount: number;
  reachedLimit: boolean;
}

interface PlanDiffRow {
  key: string;
  label: string;
  snapshotValue: unknown;
  currentValue: unknown;
  changed: boolean;
}

const defaultMetadataJson = "{}";

const statusLabels: Record<SubscriptionStatus, string> = {
  active: "Active",
  paused: "Paused",
  canceled: "Canceled",
  expired: "Expired",
};

const statusColors: Record<SubscriptionStatus, string> = {
  active: "green",
  paused: "gold",
  canceled: "default",
  expired: "red",
};

const simpleActionCopy: Record<
  SimpleSubscriptionAction,
  { label: string; okText: string; success: string; description: string; danger?: boolean }
> = {
  pause: {
    label: "暂停订阅",
    okText: "暂停",
    success: "订阅已暂停",
    description: "暂停后该订阅不再继续正常使用，恢复需重新续期或升级。",
  },
  cancel: {
    label: "取消订阅",
    okText: "取消订阅",
    success: "订阅已取消",
    description: "取消后该订阅进入 canceled 状态，请确认已和用户完成沟通。",
    danger: true,
  },
  expire: {
    label: "置为过期",
    okText: "置为过期",
    success: "订阅已置为过期",
    description: "过期操作用于测试或强制终止当前订阅周期。",
    danger: true,
  },
};

const canRunSimpleSubscriptionAction = (status: SubscriptionStatus, action: SimpleSubscriptionAction): boolean => {
  if (action === "pause") return status === "active";
  return status === "active" || status === "paused";
};

const simpleActionDisabledText: Record<SimpleSubscriptionAction, string> = {
  pause: "仅 active 订阅可暂停",
  cancel: "仅 active / paused 订阅可取消",
  expire: "仅 active / paused 订阅可置为过期",
};

const canRenewSubscription = (status: SubscriptionStatus): boolean => status !== "canceled";

const canUpgradeSubscription = (status: SubscriptionStatus): boolean => status === "active";

const billableUsageEvents = new Set(["settle", "manual_adjust"]);

const formatDateTime = (value?: string | null): string => {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
};

const formatCredits = (value?: number | null): string => {
  if (value === null || value === undefined || Number.isNaN(value)) return "-";
  return new Intl.NumberFormat(undefined, { maximumFractionDigits: 4 }).format(value);
};

const trimToNull = (value?: string | null): string | null => {
  const normalized = value?.trim();
  return normalized ? normalized : null;
};

const parseMetadataObject = (value?: string): CodePlanMetadata => {
  const text = value?.trim();
  if (!text) return {};
  const parsed = JSON.parse(text);
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("metadata 必须是 JSON object");
  }
  return parsed as CodePlanMetadata;
};

const toIsoOrNull = (value?: Dayjs | null): string | null => (value ? value.toISOString() : null);

const timeValue = (value?: string | null): number | null => {
  if (!value) return null;
  const timestamp = new Date(value).getTime();
  return Number.isNaN(timestamp) ? null : timestamp;
};

const sameInstant = (left?: string | null, right?: string | null): boolean => {
  const leftTime = timeValue(left);
  const rightTime = timeValue(right);
  if (leftTime === null || rightTime === null) return false;
  return Math.abs(leftTime - rightTime) < 1000;
};

const getDaysUntil = (value?: string | null): number | null => {
  const timestamp = timeValue(value);
  if (timestamp === null) return null;
  return Math.ceil((timestamp - Date.now()) / 86_400_000);
};

const formatExpiryHint = (subscription: CodePlanSubscription): string => {
  if (!subscription.expires_at) return "未设置到期时间";
  const days = getDaysUntil(subscription.expires_at);
  if (days === null) return formatDateTime(subscription.expires_at);
  if (days < 0) return `已超期 ${Math.abs(days)} 天`;
  if (days === 0) return "今天到期";
  return `${days} 天后到期`;
};

const valueForCompare = (value: unknown): unknown => {
  if (Array.isArray(value)) return [...value].sort();
  return value;
};

const isEqualComparable = (left: unknown, right: unknown): boolean =>
  JSON.stringify(valueForCompare(left)) === JSON.stringify(valueForCompare(right));

const formatSnapshotValue = (value: unknown): string => {
  if (value === null || value === undefined || value === "") return "-";
  if (Array.isArray(value)) return value.length ? value.join(", ") : "-";
  if (typeof value === "object") return JSON.stringify(value, null, 2);
  return String(value);
};

const activePlanOptions = (plans: CodePlanPlan[]) =>
  plans
    .filter((plan) => plan.status === "active")
    .map((plan) => ({
      label: `${plan.name} (${plan.plan_id})`,
      value: plan.plan_id,
    }));

const getEarliestWindowStart = (subscription: CodePlanSubscription): string | undefined => {
  const starts = [subscription.window_5h_start, subscription.window_week_start]
    .map(timeValue)
    .filter((value): value is number => value !== null);
  if (!starts.length) return undefined;
  return new Date(Math.min(...starts)).toISOString();
};

const matchesWindowStart = (
  entry: CodePlanUsageLedgerEntry,
  windowStart: string | null | undefined,
  windowKey: "window_5h_start" | "window_week_start",
): boolean => Boolean(windowStart) && sameInstant(entry[windowKey], windowStart);

const sumWindowCredits = (
  entries: CodePlanUsageLedgerEntry[],
  windowStart: string | null | undefined,
  windowKey: "window_5h_start" | "window_week_start",
): number | null => {
  const startTime = timeValue(windowStart);
  if (startTime === null) return null;
  return entries
    .filter((entry) => billableUsageEvents.has(entry.event_type) && matchesWindowStart(entry, windowStart, windowKey))
    .reduce((sum, entry) => sum + (Number(entry.credits) || 0), 0);
};

const buildPlanDiffRows = (snapshot: CodePlanPlanSnapshot, currentPlan?: CodePlanPlan): PlanDiffRow[] => {
  if (!currentPlan) return [];
  const comparableFields: Array<{
    key: keyof CodePlanPlanSnapshot | "plan_version";
    label: string;
    snapshotValue: unknown;
    currentValue: unknown;
  }> = [
    {
      key: "plan_version",
      label: "Plan version",
      snapshotValue: snapshot.plan_version,
      currentValue: currentPlan.version,
    },
    { key: "quota_5h", label: "5h credits 配额", snapshotValue: snapshot.quota_5h, currentValue: currentPlan.quota_5h },
    {
      key: "quota_weekly",
      label: "周 credits 配额",
      snapshotValue: snapshot.quota_weekly,
      currentValue: currentPlan.quota_weekly,
    },
    {
      key: "allowed_models",
      label: "allowed_models",
      snapshotValue: snapshot.allowed_models,
      currentValue: currentPlan.allowed_models,
    },
    { key: "rpm_limit", label: "RPM", snapshotValue: snapshot.rpm_limit, currentValue: currentPlan.rpm_limit },
    { key: "tpm_limit", label: "TPM", snapshotValue: snapshot.tpm_limit, currentValue: currentPlan.tpm_limit },
    {
      key: "max_parallel_requests",
      label: "最大并发请求",
      snapshotValue: snapshot.max_parallel_requests,
      currentValue: currentPlan.max_parallel_requests,
    },
    { key: "max_keys", label: "max_keys", snapshotValue: snapshot.max_keys, currentValue: currentPlan.max_keys },
    {
      key: "default_max_output_tokens",
      label: "默认 max output tokens",
      snapshotValue: snapshot.default_max_output_tokens,
      currentValue: currentPlan.default_max_output_tokens,
    },
    {
      key: "credit_rule_id",
      label: "credit_rule_id",
      snapshotValue: snapshot.credit_rule_id,
      currentValue: currentPlan.credit_rule_id,
    },
  ];

  return comparableFields.map((field) => ({
    key: String(field.key),
    label: field.label,
    snapshotValue: field.snapshotValue,
    currentValue: field.currentValue,
    changed: !isEqualComparable(field.snapshotValue, field.currentValue),
  }));
};

const hasPlanDrift = (subscription: CodePlanSubscription, currentPlan?: CodePlanPlan): boolean =>
  !currentPlan || buildPlanDiffRows(subscription.plan_snapshot, currentPlan).some((row) => row.changed);

const keyCount = (subscription: CodePlanSubscription): number => subscription.litellm_key_ids?.length ?? 0;

const keyUsageLabel = (subscription: CodePlanSubscription): string =>
  `${keyCount(subscription)} / ${subscription.plan_snapshot.max_keys}`;

const keyUsagePercent = (subscription: CodePlanSubscription): number => {
  const maxKeys = subscription.plan_snapshot.max_keys || 0;
  if (!maxKeys) return 0;
  return Math.min(100, Math.round((keyCount(subscription) / maxKeys) * 100));
};

const quotaPercent = (used: number | null | undefined, quota: number): number | null => {
  if (used === null || used === undefined || quota <= 0) return null;
  return Math.min(100, Math.max(0, Math.round((used / quota) * 100)));
};

function CodePlanSubscriptionsFrame({ children }: { children: ReactNode }) {
  const section = CODE_PLAN_PAGE_DEFINITIONS.subscriptions;
  return (
    <div className="mx-auto flex w-full max-w-7xl flex-col gap-5 p-4 lg:p-6">
      <CodePlanBreadcrumb title={section.title} />
      <div className="flex flex-col gap-1">
        <Title level={2} className="!mb-0">
          {section.title}
        </Title>
        <Text>{section.description}</Text>
      </div>
      {children}
    </div>
  );
}

function SubscriptionStatusTag({ status }: { status: SubscriptionStatus }) {
  return <Tag color={statusColors[status]}>{statusLabels[status]}</Tag>;
}

function MetricTile({
  label,
  value,
  hint,
  tone = "neutral",
}: {
  label: string;
  value: ReactNode;
  hint: ReactNode;
  tone?: "neutral" | "good" | "warning" | "danger";
}) {
  const toneClass = {
    neutral: "border-slate-200 bg-white",
    good: "border-emerald-200 bg-emerald-50",
    warning: "border-amber-200 bg-amber-50",
    danger: "border-rose-200 bg-rose-50",
  }[tone];

  return (
    <div className={`rounded-lg border px-4 py-3 ${toneClass}`}>
      <Text type="secondary" className="text-xs">
        {label}
      </Text>
      <div className="mt-1 break-words text-2xl font-semibold text-slate-950">{value}</div>
      <div className="mt-1 text-xs text-slate-500">{hint}</div>
    </div>
  );
}

function ProvisionResultModal({
  result,
  onClose,
}: {
  result: CodePlanSubscriptionProvisionResponse | null;
  onClose: () => void;
}) {
  const clearAndClose = () => onClose();
  return (
    <Modal
      title="Provision 结果"
      open={Boolean(result)}
      onCancel={clearAndClose}
      destroyOnClose
      footer={
        <Button type="primary" onClick={clearAndClose}>
          关闭并清除
        </Button>
      }
    >
      {result ? (
        <Space direction="vertical" size={12} className="w-full">
          {result.key ? (
            <Alert
              type="warning"
              showIcon
              icon={<EyeInvisibleOutlined />}
              message="明文 Key 仅本次展示"
              description="关闭此弹窗后前端不会再保留该明文 Key；后续只能重新代发或轮换。"
            />
          ) : (
            <Alert type="info" showIcon message="后端未返回明文 Key" />
          )}
          <Descriptions size="small" column={1} bordered>
            <Descriptions.Item label="subscription_id">
              <Text code>{result.subscription.subscription_id}</Text>
            </Descriptions.Item>
            <Descriptions.Item label="litellm_team_id">
              <Text code>{result.subscription.litellm_team_id ?? "-"}</Text>
            </Descriptions.Item>
            <Descriptions.Item label="key_id">
              <Text code>{result.key_id ?? "-"}</Text>
            </Descriptions.Item>
            <Descriptions.Item label="token_id">
              <Text code>{result.token_id ?? "-"}</Text>
            </Descriptions.Item>
          </Descriptions>
          {result.key ? (
            <Space direction="vertical" size={8} className="w-full">
              <Input.TextArea value={result.key} readOnly rows={4} />
              <Paragraph copyable={{ text: result.key }} className="!mb-0">
                复制明文 Key
              </Paragraph>
            </Space>
          ) : null}
        </Space>
      ) : null}
    </Modal>
  );
}

function PlanSnapshotDiff({
  subscription,
  currentPlan,
}: {
  subscription: CodePlanSubscription;
  currentPlan?: CodePlanPlan;
}) {
  const snapshot = subscription.plan_snapshot;
  const rows = buildPlanDiffRows(snapshot, currentPlan);
  const changedRows = rows.filter((row) => row.changed);
  let snapshotStateAlert: ReactNode;

  if (!currentPlan) {
    snapshotStateAlert = (
      <Alert
        type="warning"
        showIcon
        message="当前 Plan 模板不可用"
        description="该订阅仍按 plan_snapshot 运行；当前 Plan 可能已被归档、删除或不在返回列表中。"
      />
    );
  } else if (changedRows.length) {
    snapshotStateAlert = (
      <Alert
        type="warning"
        showIcon
        message={`该订阅正在使用旧权益，${changedRows.length} 个字段和当前 Plan 不一致`}
      />
    );
  } else {
    snapshotStateAlert = <Alert type="success" showIcon message="plan_snapshot 与当前 Plan 模板一致" />;
  }

  const columns: TableProps<PlanDiffRow>["columns"] = [
    { title: "字段", dataIndex: "label", key: "label", width: 170 },
    {
      title: "订阅快照",
      dataIndex: "snapshotValue",
      key: "snapshotValue",
      render: (value) => <Text className="whitespace-pre-wrap">{formatSnapshotValue(value)}</Text>,
    },
    {
      title: "当前 Plan",
      dataIndex: "currentValue",
      key: "currentValue",
      render: (value) => <Text className="whitespace-pre-wrap">{formatSnapshotValue(value)}</Text>,
    },
    {
      title: "状态",
      key: "changed",
      width: 100,
      render: (_, row) => (row.changed ? <Tag color="warning">旧权益</Tag> : <Tag>一致</Tag>),
    },
  ];

  return (
    <Space direction="vertical" size={12} className="w-full">
      <Descriptions size="small" bordered column={2}>
        <Descriptions.Item label="snapshot plan">
          {snapshot.name} <Text code>{snapshot.plan_id}</Text>
        </Descriptions.Item>
        <Descriptions.Item label="captured_at">{formatDateTime(snapshot.captured_at)}</Descriptions.Item>
        <Descriptions.Item label="credit rule version">{snapshot.credit_rule_version ?? "-"}</Descriptions.Item>
        <Descriptions.Item label="credit multipliers">
          input {snapshot.credit_input_multiplier} / output {snapshot.credit_output_multiplier} / cache read{" "}
          {snapshot.credit_cache_read_multiplier} / cache write {snapshot.credit_cache_write_multiplier}
        </Descriptions.Item>
      </Descriptions>
      {snapshotStateAlert}
      {currentPlan ? (
        <Table<PlanDiffRow>
          rowKey="key"
          columns={columns}
          dataSource={rows}
          pagination={false}
          size="small"
          scroll={{ x: 760 }}
        />
      ) : null}
    </Space>
  );
}

function QuotaMeter({
  label,
  used,
  quota,
  remaining,
  loading,
}: {
  label: string;
  used: number | null | undefined;
  quota: number;
  remaining: number | null;
  loading: boolean;
}) {
  const percent = quotaPercent(used, quota);
  const status = percent !== null && percent >= 95 ? "exception" : percent !== null && percent >= 80 ? "normal" : "success";

  return (
    <div className="rounded-lg border border-slate-200 bg-white px-4 py-3">
      <div className="mb-2 flex items-center justify-between gap-3">
        <Text strong>{label}</Text>
        <Text type="secondary">{loading ? "加载中" : `${formatCredits(used)} / ${formatCredits(quota)}`}</Text>
      </div>
      <Progress percent={percent ?? 0} status={status} showInfo={percent !== null} />
      <div className="mt-2 flex items-center justify-between text-xs text-slate-500">
        <span>剩余 {loading ? "加载中" : formatCredits(remaining)}</span>
        <span>{percent === null ? "窗口未开始" : `已用 ${percent}%`}</span>
      </div>
    </div>
  );
}

function QuotaEstimateView({
  subscription,
  usageEstimate,
  loading,
}: {
  subscription: CodePlanSubscription;
  usageEstimate: UsageEstimate | null;
  loading: boolean;
}) {
  const snapshot = subscription.plan_snapshot;
  const fiveHourRemaining =
    usageEstimate?.fiveHourUsed === null || usageEstimate?.fiveHourUsed === undefined
      ? null
      : snapshot.quota_5h - usageEstimate.fiveHourUsed;
  const weekRemaining =
    usageEstimate?.weekUsed === null || usageEstimate?.weekUsed === undefined
      ? null
      : snapshot.quota_weekly - usageEstimate.weekUsed;

  return (
    <Space direction="vertical" size={12} className="w-full">
      <Alert
        type="info"
        showIcon
        message="额度余量为账本推算值，非 Redis 实时值"
        description="后端暂无 /v1/admin/quota/* 只读接口，本页仅用 usage-ledger 中 settle/manual_adjust 事件推算当前窗口已用。"
      />
      <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
        <QuotaMeter
          label="5h window"
          used={usageEstimate?.fiveHourUsed}
          quota={snapshot.quota_5h}
          remaining={fiveHourRemaining}
          loading={loading}
        />
        <QuotaMeter
          label="week window"
          used={usageEstimate?.weekUsed}
          quota={snapshot.quota_weekly}
          remaining={weekRemaining}
          loading={loading}
        />
      </div>
      <Descriptions size="small" bordered column={2}>
        <Descriptions.Item label="5h window start">{formatDateTime(subscription.window_5h_start)}</Descriptions.Item>
        <Descriptions.Item label="week window start">
          {formatDateTime(subscription.window_week_start)}
        </Descriptions.Item>
        <Descriptions.Item label="参与推算事件数">
          {loading ? "加载中" : usageEstimate?.eventCount ?? 0}
        </Descriptions.Item>
        <Descriptions.Item label="结果范围">
          {usageEstimate?.reachedLimit ? <Tag color="warning">达到 1000 条上限</Tag> : <Tag>完整返回范围</Tag>}
        </Descriptions.Item>
      </Descriptions>
    </Space>
  );
}

function SubscriptionSummary({
  subscription,
  currentPlan,
}: {
  subscription: CodePlanSubscription;
  currentPlan?: CodePlanPlan;
}) {
  const drift = hasPlanDrift(subscription, currentPlan);
  const keysAtLimit = keyCount(subscription) >= subscription.plan_snapshot.max_keys;

  return (
    <div className="rounded-lg border border-slate-200 bg-slate-50 px-4 py-4">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <SubscriptionStatusTag status={subscription.status} />
            {drift ? <Tag color="warning">旧权益</Tag> : <Tag color="success">权益同步</Tag>}
            {keysAtLimit ? <Tag color="warning">Key 已达上限</Tag> : null}
          </div>
          <Title level={4} className="!mb-1 !mt-3 break-all">
            {subscription.plan_snapshot.name}
          </Title>
          <Text type="secondary" className="break-all">
            {subscription.project_id} / <Text code>{subscription.subscription_id}</Text>
          </Text>
        </div>
        <div className="grid w-full grid-cols-2 gap-3 lg:w-auto lg:min-w-[320px]">
          <MetricTile label="到期" value={formatExpiryHint(subscription)} hint={formatDateTime(subscription.expires_at)} />
          <MetricTile label="Key 用量" value={keyUsageLabel(subscription)} hint={`上限 ${subscription.plan_snapshot.max_keys}`} />
        </div>
      </div>
    </div>
  );
}

function CodePlanSubscriptionsManager({ accessToken }: { accessToken?: string | null }) {
  const [createForm] = Form.useForm<CreateSubscriptionFormValues>();
  const [renewForm] = Form.useForm<RenewSubscriptionFormValues>();
  const [upgradeForm] = Form.useForm<UpgradeSubscriptionFormValues>();
  const [issueKeyForm] = Form.useForm<IssueKeyFormValues>();
  const [revokeKeyForm] = Form.useForm<RevokeKeyFormValues>();
  const [messageApi, messageContextHolder] = message.useMessage();
  const [modalApi, modalContextHolder] = Modal.useModal();

  const [plans, setPlans] = useState<CodePlanPlan[]>([]);
  const [subscriptions, setSubscriptions] = useState<CodePlanSubscription[]>([]);
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const [projectFilter, setProjectFilter] = useState("");
  const [submittedProjectFilter, setSubmittedProjectFilter] = useState("");
  const [planFilter, setPlanFilter] = useState<string | undefined>();
  const [loadingSubscriptions, setLoadingSubscriptions] = useState(false);
  const [loadingPlans, setLoadingPlans] = useState(false);
  const [subscriptionsError, setSubscriptionsError] = useState<string | null>(null);
  const [plansError, setPlansError] = useState<string | null>(null);
  const [createDrawerOpen, setCreateDrawerOpen] = useState(false);
  const [detailDrawerOpen, setDetailDrawerOpen] = useState(false);
  const [selectedSubscription, setSelectedSubscription] = useState<CodePlanSubscription | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [usageEstimate, setUsageEstimate] = useState<UsageEstimate | null>(null);
  const [usageLoading, setUsageLoading] = useState(false);
  const [provisionResult, setProvisionResult] = useState<CodePlanSubscriptionProvisionResponse | null>(null);
  const [operationKey, setOperationKey] = useState<string | null>(null);

  const activePlans = useMemo(() => plans.filter((plan) => plan.status === "active"), [plans]);
  const planById = useMemo(() => new Map(plans.map((plan) => [plan.plan_id, plan])), [plans]);
  const allPlanOptions = useMemo(
    () => plans.map((plan) => ({ label: `${plan.name} (${plan.plan_id})`, value: plan.plan_id })),
    [plans],
  );
  const activePlanSelectOptions = useMemo(() => activePlanOptions(plans), [plans]);
  const currentPlan = useMemo(
    () => plans.find((plan) => plan.plan_id === selectedSubscription?.plan_id),
    [plans, selectedSubscription?.plan_id],
  );
  const summary = useMemo(() => {
    const statusCounts: Record<SubscriptionStatus, number> = {
      active: 0,
      paused: 0,
      canceled: 0,
      expired: 0,
    };
    let noKeyCount = 0;
    let expiringSoonCount = 0;
    let driftCount = 0;

    subscriptions.forEach((subscription) => {
      statusCounts[subscription.status] += 1;
      if (keyCount(subscription) === 0) noKeyCount += 1;
      const days = getDaysUntil(subscription.expires_at);
      if (subscription.status === "active" && days !== null && days >= 0 && days <= 7) {
        expiringSoonCount += 1;
      }
      if (plans.length && hasPlanDrift(subscription, planById.get(subscription.plan_id))) {
        driftCount += 1;
      }
    });

    return { statusCounts, noKeyCount, expiringSoonCount, driftCount };
  }, [planById, plans.length, subscriptions]);

  const loadPlans = useCallback(async () => {
    if (!accessToken) return;
    setLoadingPlans(true);
    setPlansError(null);
    try {
      const response = await listCodePlanPlans(accessToken);
      setPlans(response.data ?? []);
    } catch (error) {
      setPlansError(formatCodePlanError(error));
    } finally {
      setLoadingPlans(false);
    }
  }, [accessToken]);

  const loadSubscriptions = useCallback(async () => {
    if (!accessToken) return;
    setLoadingSubscriptions(true);
    setSubscriptionsError(null);
    try {
      const response = await listCodePlanSubscriptions(accessToken, {
        status: statusFilter === "all" ? undefined : statusFilter,
        project_id: trimToNull(submittedProjectFilter),
        plan_id: planFilter,
      });
      setSubscriptions(response.data ?? []);
    } catch (error) {
      setSubscriptionsError(formatCodePlanError(error));
    } finally {
      setLoadingSubscriptions(false);
    }
  }, [accessToken, planFilter, statusFilter, submittedProjectFilter]);

  const updateSubscriptionInList = useCallback((subscription: CodePlanSubscription) => {
    setSubscriptions((previous) => {
      if (previous.some((item) => item.subscription_id === subscription.subscription_id)) {
        return previous.map((item) => (item.subscription_id === subscription.subscription_id ? subscription : item));
      }
      return [subscription, ...previous];
    });
  }, []);

  const loadUsageEstimate = useCallback(
    async (subscription: CodePlanSubscription) => {
      if (!accessToken) return;
      const startTime = getEarliestWindowStart(subscription);
      if (!startTime) {
        setUsageEstimate({ fiveHourUsed: null, weekUsed: null, eventCount: 0, reachedLimit: false });
        return;
      }
      setUsageLoading(true);
      try {
        const response = await listCodePlanUsageLedger(accessToken, {
          subscription_id: subscription.subscription_id,
          start_time: startTime,
          limit: 1000,
        });
        const entries = response.data ?? [];
        const billableWindowEntries = entries
          .filter((entry) => billableUsageEvents.has(entry.event_type))
          .filter(
            (entry) =>
              matchesWindowStart(entry, subscription.window_5h_start, "window_5h_start") ||
              matchesWindowStart(entry, subscription.window_week_start, "window_week_start"),
          );
        setUsageEstimate({
          fiveHourUsed: sumWindowCredits(entries, subscription.window_5h_start, "window_5h_start"),
          weekUsed: sumWindowCredits(entries, subscription.window_week_start, "window_week_start"),
          eventCount: billableWindowEntries.length,
          reachedLimit: entries.length >= 1000,
        });
      } catch (error) {
        messageApi.warning(formatCodePlanError(error));
        setUsageEstimate(null);
      } finally {
        setUsageLoading(false);
      }
    },
    [accessToken, messageApi],
  );

  const loadSubscriptionDetail = useCallback(
    async (subscriptionId: string) => {
      if (!accessToken) return;
      setDetailLoading(true);
      try {
        const detail = await getCodePlanSubscription(accessToken, subscriptionId);
        setSelectedSubscription(detail);
        updateSubscriptionInList(detail);
        await loadUsageEstimate(detail);
        renewForm.setFieldsValue({
          expires_at: detail.expires_at ? dayjs(detail.expires_at) : null,
          issue_key: false,
          key_alias: undefined,
          metadata: defaultMetadataJson,
        });
        upgradeForm.setFieldsValue({
          plan_id: undefined,
          expires_at: detail.expires_at ? dayjs(detail.expires_at) : null,
          issue_key: false,
          key_alias: undefined,
          metadata: defaultMetadataJson,
        });
        issueKeyForm.setFieldsValue({ key_alias: undefined, metadata: defaultMetadataJson });
        revokeKeyForm.setFieldsValue({ key_id: undefined });
      } catch (error) {
        messageApi.error(formatCodePlanError(error));
      } finally {
        setDetailLoading(false);
      }
    },
    [
      accessToken,
      issueKeyForm,
      loadUsageEstimate,
      messageApi,
      renewForm,
      revokeKeyForm,
      updateSubscriptionInList,
      upgradeForm,
    ],
  );

  useEffect(() => {
    const timeoutId = window.setTimeout(() => {
      void loadPlans();
    }, 0);

    return () => window.clearTimeout(timeoutId);
  }, [loadPlans]);

  useEffect(() => {
    const timeoutId = window.setTimeout(() => {
      void loadSubscriptions();
    }, 0);

    return () => window.clearTimeout(timeoutId);
  }, [loadSubscriptions]);

  const applyProjectFilter = useCallback(() => {
    const nextProjectFilter = projectFilter.trim();
    if (nextProjectFilter === submittedProjectFilter) {
      void loadSubscriptions();
      return;
    }
    setSubmittedProjectFilter(nextProjectFilter);
  }, [loadSubscriptions, projectFilter, submittedProjectFilter]);

  const openCreateDrawer = () => {
    createForm.setFieldsValue({
      issue_key: true,
      metadata: defaultMetadataJson,
      expires_at: null,
      key_alias: undefined,
      plan_id: undefined,
      project_id: undefined,
    });
    setCreateDrawerOpen(true);
  };

  const closeCreateDrawer = () => {
    setCreateDrawerOpen(false);
    createForm.resetFields();
  };

  const openDetailDrawer = useCallback(
    (subscription: CodePlanSubscription) => {
      setSelectedSubscription(subscription);
      setDetailDrawerOpen(true);
      void loadSubscriptionDetail(subscription.subscription_id);
    },
    [loadSubscriptionDetail],
  );

  const closeDetailDrawer = () => {
    setDetailDrawerOpen(false);
    setSelectedSubscription(null);
    setUsageEstimate(null);
    renewForm.resetFields();
    upgradeForm.resetFields();
    issueKeyForm.resetFields();
    revokeKeyForm.resetFields();
  };

  const handleOperationError = async (error: unknown, subscriptionId?: string) => {
    messageApi.error(formatCodePlanError(error));
    if (isCodePlanVersionConflict(error) && subscriptionId) {
      await loadSubscriptionDetail(subscriptionId);
    }
  };

  const handleProvisionResponse = async (response: CodePlanSubscriptionProvisionResponse) => {
    updateSubscriptionInList(response.subscription);
    if (selectedSubscription?.subscription_id === response.subscription.subscription_id) {
      setSelectedSubscription(response.subscription);
      await loadUsageEstimate(response.subscription);
    }
    setProvisionResult(response);
  };

  const submitCreateSubscription = async () => {
    let values: CreateSubscriptionFormValues;
    try {
      values = await createForm.validateFields();
    } catch {
      return;
    }

    let metadata: CodePlanMetadata;
    try {
      metadata = parseMetadataObject(values.metadata);
    } catch {
      createForm.setFields([{ name: "metadata", errors: ["metadata 必须是 JSON object"] }]);
      return;
    }

    setOperationKey("create");
    try {
      const response = await createCodePlanSubscription(accessToken, {
        project_id: values.project_id!.trim(),
        plan_id: values.plan_id!,
        expires_at: toIsoOrNull(values.expires_at),
        issue_key: values.issue_key ?? true,
        key_alias: trimToNull(values.key_alias),
        metadata,
      });
      messageApi.success("订阅已开通");
      closeCreateDrawer();
      await handleProvisionResponse(response);
      await loadSubscriptions();
    } catch (error) {
      messageApi.error(formatCodePlanError(error));
    } finally {
      setOperationKey(null);
    }
  };

  const runSimpleAction = (action: SimpleSubscriptionAction) => {
    if (!selectedSubscription) return;
    if (!canRunSimpleSubscriptionAction(selectedSubscription.status, action)) {
      messageApi.warning(simpleActionDisabledText[action]);
      return;
    }
    const copy = simpleActionCopy[action];
    modalApi.confirm({
      title: `${copy.label}？`,
      okText: copy.okText,
      cancelText: "取消",
      okButtonProps: { danger: copy.danger },
      content: (
        <Space direction="vertical" size={8}>
          <Text>{copy.description}</Text>
          <Text type="secondary">本次操作会带上当前版本号 v{selectedSubscription.version} 做乐观锁校验。</Text>
        </Space>
      ),
      onOk: async () => {
        if (!selectedSubscription) return;
        setOperationKey(action);
        try {
          const body = { version: selectedSubscription.version };
          let next: CodePlanSubscription;

          if (action === "pause") {
            next = await pauseCodePlanSubscription(accessToken, selectedSubscription.subscription_id, body);
          } else if (action === "cancel") {
            next = await cancelCodePlanSubscription(accessToken, selectedSubscription.subscription_id, body);
          } else {
            next = await expireCodePlanSubscription(accessToken, selectedSubscription.subscription_id, body);
          }

          messageApi.success(copy.success);
          setSelectedSubscription(next);
          updateSubscriptionInList(next);
          await loadUsageEstimate(next);
          await loadSubscriptions();
        } catch (error) {
          await handleOperationError(error, selectedSubscription.subscription_id);
          throw error;
        } finally {
          setOperationKey(null);
        }
      },
    });
  };

  const submitRenewSubscription = async () => {
    if (!selectedSubscription) return;
    let values: RenewSubscriptionFormValues;
    try {
      values = await renewForm.validateFields();
    } catch {
      return;
    }
    let metadata: CodePlanMetadata;
    try {
      metadata = parseMetadataObject(values.metadata);
    } catch {
      renewForm.setFields([{ name: "metadata", errors: ["metadata 必须是 JSON object"] }]);
      return;
    }

    modalApi.confirm({
      title: "确认续期？",
      okText: "续期",
      cancelText: "取消",
      content: (
        <Space direction="vertical" size={8}>
          <Text>续期会刷新订阅周期，并可按需同时下发一个新 Key。</Text>
          <Text type="secondary">本次操作会带上当前版本号 v{selectedSubscription.version} 做乐观锁校验。</Text>
        </Space>
      ),
      onOk: async () => {
        if (!selectedSubscription) return;
        setOperationKey("renew");
        try {
          const response = await renewCodePlanSubscription(accessToken, selectedSubscription.subscription_id, {
            version: selectedSubscription.version,
            expires_at: toIsoOrNull(values.expires_at),
            issue_key: values.issue_key ?? false,
            key_alias: trimToNull(values.key_alias),
            metadata,
          });
          messageApi.success("订阅已续期");
          await handleProvisionResponse(response);
          await loadSubscriptions();
        } catch (error) {
          await handleOperationError(error, selectedSubscription.subscription_id);
          throw error;
        } finally {
          setOperationKey(null);
        }
      },
    });
  };

  const submitUpgradeSubscription = async () => {
    if (!selectedSubscription) return;
    let values: UpgradeSubscriptionFormValues;
    try {
      values = await upgradeForm.validateFields();
    } catch {
      return;
    }
    let metadata: CodePlanMetadata;
    try {
      metadata = parseMetadataObject(values.metadata);
    } catch {
      upgradeForm.setFields([{ name: "metadata", errors: ["metadata 必须是 JSON object"] }]);
      return;
    }

    modalApi.confirm({
      title: "确认升降级？",
      okText: "提交",
      cancelText: "取消",
      content: (
        <Space direction="vertical" size={8}>
          <Text>升级即时生效并按新权益重算窗口上限；降级当前周期结束后生效。</Text>
          <Text type="secondary">本次操作会带上当前版本号 v{selectedSubscription.version} 做乐观锁校验。</Text>
        </Space>
      ),
      onOk: async () => {
        if (!selectedSubscription) return;
        setOperationKey("upgrade");
        try {
          const response = await upgradeCodePlanSubscription(accessToken, selectedSubscription.subscription_id, {
            version: selectedSubscription.version,
            plan_id: values.plan_id!,
            expires_at: toIsoOrNull(values.expires_at),
            issue_key: values.issue_key ?? false,
            key_alias: trimToNull(values.key_alias),
            metadata,
          });
          messageApi.success("订阅已升降级");
          await handleProvisionResponse(response);
          await loadSubscriptions();
        } catch (error) {
          await handleOperationError(error, selectedSubscription.subscription_id);
          throw error;
        } finally {
          setOperationKey(null);
        }
      },
    });
  };

  const submitIssueKey = async () => {
    if (!selectedSubscription) return;
    let values: IssueKeyFormValues;
    try {
      values = await issueKeyForm.validateFields();
    } catch {
      return;
    }
    let metadata: CodePlanMetadata;
    try {
      metadata = parseMetadataObject(values.metadata);
    } catch {
      issueKeyForm.setFields([{ name: "metadata", errors: ["metadata 必须是 JSON object"] }]);
      return;
    }

    setOperationKey("issue-key");
    try {
      const response = await issueCodePlanSubscriptionKey(accessToken, selectedSubscription.subscription_id, {
        version: selectedSubscription.version,
        key_alias: trimToNull(values.key_alias),
        metadata,
      });
      messageApi.success("Key 已代发");
      await handleProvisionResponse(response);
      await loadSubscriptions();
      issueKeyForm.setFieldsValue({ key_alias: undefined, metadata: defaultMetadataJson });
    } catch (error) {
      await handleOperationError(error, selectedSubscription.subscription_id);
    } finally {
      setOperationKey(null);
    }
  };

  const submitRevokeKey = async () => {
    if (!selectedSubscription) return;
    let values: RevokeKeyFormValues;
    try {
      values = await revokeKeyForm.validateFields();
    } catch {
      return;
    }

    modalApi.confirm({
      title: "确认吊销 Key？",
      okText: "吊销",
      cancelText: "取消",
      okButtonProps: { danger: true },
      content: (
        <Space direction="vertical" size={8}>
          <Text>
            Key <Text code>{values.key_id}</Text> 吊销后不可恢复。
          </Text>
          <Text type="secondary">增删/轮换 Key 不会重置 5h 或周窗口额度。</Text>
        </Space>
      ),
      onOk: async () => {
        if (!selectedSubscription) return;
        setOperationKey("revoke-key");
        try {
          const next = await revokeCodePlanSubscriptionKey(accessToken, selectedSubscription.subscription_id, {
            version: selectedSubscription.version,
            key_id: values.key_id!,
          });
          messageApi.success("Key 已吊销");
          setSelectedSubscription(next);
          updateSubscriptionInList(next);
          await loadUsageEstimate(next);
          await loadSubscriptions();
          revokeKeyForm.resetFields();
        } catch (error) {
          await handleOperationError(error, selectedSubscription.subscription_id);
          throw error;
        } finally {
          setOperationKey(null);
        }
      },
    });
  };

  const columns = useMemo<TableProps<CodePlanSubscription>["columns"]>(
    () => [
      {
        title: "订阅",
        dataIndex: "subscription_id",
        key: "subscription_id",
        width: 300,
        render: (subscriptionId: string, subscription) => (
          <Space direction="vertical" size={2} className="min-w-0">
            <Button type="link" className="!h-auto !p-0 text-left" onClick={() => openDetailDrawer(subscription)}>
              <Text code className="break-all">
                {subscriptionId}
              </Text>
            </Button>
            <Text type="secondary" className="break-all">
              {subscription.project_id}
            </Text>
            {subscription.litellm_team_id ? (
              <Text type="secondary" code className="break-all">
                {subscription.litellm_team_id}
              </Text>
            ) : null}
          </Space>
        ),
      },
      {
        title: "Plan / 权益",
        dataIndex: "plan_id",
        key: "plan_id",
        width: 280,
        render: (_: string, subscription) => {
          const drift = plans.length > 0 && hasPlanDrift(subscription, planById.get(subscription.plan_id));
          return (
            <Space direction="vertical" size={4}>
              <Space wrap size={4}>
                <Text strong>{subscription.plan_snapshot?.name ?? subscription.plan_id}</Text>
                {drift ? <Tag color="warning">旧权益</Tag> : null}
              </Space>
              <Text type="secondary" code className="break-all">
                {subscription.plan_id}
              </Text>
              <Text type="secondary">
                5h {formatCredits(subscription.plan_snapshot.quota_5h)} / week{" "}
                {formatCredits(subscription.plan_snapshot.quota_weekly)}
              </Text>
            </Space>
          );
        },
      },
      {
        title: "状态 / 到期",
        dataIndex: "status",
        key: "status",
        width: 180,
        render: (status: SubscriptionStatus, subscription) => (
          <Space direction="vertical" size={4}>
            <SubscriptionStatusTag status={status} />
            <Text>{formatExpiryHint(subscription)}</Text>
            <Text type="secondary">{formatDateTime(subscription.expires_at)}</Text>
          </Space>
        ),
      },
      {
        title: "Key",
        dataIndex: "litellm_key_ids",
        key: "litellm_key_ids",
        width: 150,
        render: (_: string[], subscription) => (
          <Space direction="vertical" size={4} className="w-full">
            <Text>{keyUsageLabel(subscription)}</Text>
            <Progress percent={keyUsagePercent(subscription)} size="small" showInfo={false} />
            {keyCount(subscription) >= subscription.plan_snapshot.max_keys ? <Tag color="warning">已满</Tag> : null}
          </Space>
        ),
      },
      {
        title: "窗口锚点",
        key: "windows",
        width: 260,
        render: (_, subscription) => (
          <Space direction="vertical" size={0}>
            <Text type="secondary">5h: {formatDateTime(subscription.window_5h_start)}</Text>
            <Text type="secondary">week: {formatDateTime(subscription.window_week_start)}</Text>
          </Space>
        ),
      },
      {
        title: "操作",
        key: "actions",
        fixed: "right",
        width: 120,
        render: (_, subscription) => (
          <Tooltip title="查看详情与运营动作">
            <Button size="small" onClick={() => openDetailDrawer(subscription)}>
              详情
            </Button>
          </Tooltip>
        ),
      },
    ],
    [openDetailDrawer, planById, plans.length],
  );
  const canPauseSelectedSubscription = selectedSubscription
    ? canRunSimpleSubscriptionAction(selectedSubscription.status, "pause")
    : false;
  const canCancelSelectedSubscription = selectedSubscription
    ? canRunSimpleSubscriptionAction(selectedSubscription.status, "cancel")
    : false;
  const canExpireSelectedSubscription = selectedSubscription
    ? canRunSimpleSubscriptionAction(selectedSubscription.status, "expire")
    : false;
  const canRenewSelectedSubscription = selectedSubscription ? canRenewSubscription(selectedSubscription.status) : false;
  const canUpgradeSelectedSubscription = selectedSubscription
    ? canUpgradeSubscription(selectedSubscription.status)
    : false;
  const canIssueSelectedKey = selectedSubscription
    ? selectedSubscription.status === "active" && keyCount(selectedSubscription) < selectedSubscription.plan_snapshot.max_keys
    : false;
  const emptyDetailDrawerContent = detailLoading ? <Text>加载中</Text> : null;

  return (
    <Space direction="vertical" size={16} className="w-full">
      {messageContextHolder}
      {modalContextHolder}
      {subscriptionsError ? <Alert type="error" showIcon message={subscriptionsError} /> : null}
      {plansError ? <Alert type="warning" showIcon message={plansError} /> : null}

      <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-4">
        <MetricTile
          label="当前列表"
          value={subscriptions.length}
          hint={`Active ${summary.statusCounts.active} / Paused ${summary.statusCounts.paused}`}
          tone="neutral"
        />
        <MetricTile
          label="可用订阅"
          value={summary.statusCounts.active}
          hint="可续期、升降级、代发 Key"
          tone="good"
        />
        <MetricTile
          label="7 天内到期"
          value={summary.expiringSoonCount}
          hint="优先处理续期或取消"
          tone={summary.expiringSoonCount ? "warning" : "neutral"}
        />
        <MetricTile
          label="需要关注"
          value={summary.noKeyCount + summary.driftCount}
          hint={`无 Key ${summary.noKeyCount} / 旧权益 ${summary.driftCount}`}
          tone={summary.noKeyCount + summary.driftCount ? "warning" : "neutral"}
        />
      </div>

      <Card className="rounded-lg border border-slate-200 shadow-sm" bodyStyle={{ padding: 16 }}>
        <div className="mb-4 flex flex-col gap-3 xl:flex-row xl:items-center xl:justify-between">
          <Space wrap>
            <Segmented
              value={statusFilter}
              onChange={(value) => setStatusFilter(value as StatusFilter)}
              options={[
                { label: "全部", value: "all" },
                { label: "Active", value: "active" },
                { label: "Paused", value: "paused" },
                { label: "Canceled", value: "canceled" },
                { label: "Expired", value: "expired" },
              ]}
            />
            <Input
              allowClear
              className="w-[220px]"
              placeholder="project_id"
              value={projectFilter}
              onChange={(event) => setProjectFilter(event.target.value)}
              onPressEnter={() => applyProjectFilter()}
            />
            <Button onClick={() => applyProjectFilter()} loading={loadingSubscriptions}>
              查询
            </Button>
            <Select
              allowClear
              showSearch
              className="w-[260px]"
              optionFilterProp="label"
              placeholder="plan_id"
              value={planFilter}
              options={allPlanOptions}
              loading={loadingPlans}
              onChange={(value) => setPlanFilter(value)}
            />
          </Space>
          <Space wrap>
            <Button icon={<ReloadOutlined />} onClick={loadSubscriptions} loading={loadingSubscriptions}>
              刷新
            </Button>
            <Button type="primary" icon={<PlusOutlined />} onClick={openCreateDrawer} disabled={!activePlans.length}>
              开通订阅
            </Button>
          </Space>
        </div>
        <Table<CodePlanSubscription>
          rowKey="subscription_id"
          columns={columns}
          dataSource={subscriptions}
          loading={loadingSubscriptions}
          scroll={{ x: 1290 }}
          pagination={{ pageSize: 10, showSizeChanger: true }}
          locale={{
            emptyText: (
              <CodePlanEmptyState
                title="暂无订阅"
                description="开通订阅后可在这里处理续期、状态机、升降级和 Key 操作。"
              />
            ),
          }}
        />
      </Card>

      <Drawer
        title="开通订阅"
        open={createDrawerOpen}
        width={560}
        destroyOnClose
        onClose={closeCreateDrawer}
        footer={
          <div className="flex justify-end gap-2">
            <Button onClick={closeCreateDrawer}>取消</Button>
            <Button type="primary" loading={operationKey === "create"} onClick={submitCreateSubscription}>
              开通
            </Button>
          </div>
        }
      >
        <Space direction="vertical" size={16} className="w-full">
          <Alert
            type="info"
            showIcon
            message="开通会按当前 active Plan 生成不可变 plan_snapshot"
            description="如勾选下发 Key，明文 Key 只会在提交成功后的弹窗展示一次。"
          />
          <Form<CreateSubscriptionFormValues>
            form={createForm}
            layout="vertical"
            initialValues={{ issue_key: true, metadata: defaultMetadataJson }}
          >
            <Form.Item name="plan_id" label="Plan" rules={[{ required: true, message: "请选择 active Plan" }]}>
              <Select
                showSearch
                optionFilterProp="label"
                options={activePlanSelectOptions}
                loading={loadingPlans}
                placeholder="仅可选择 active Plan"
              />
            </Form.Item>
            <Form.Item
              name="project_id"
              label="用户 / project_id"
              rules={[{ required: true, message: "请输入 project_id" }]}
            >
              <Input maxLength={255} placeholder="project id" />
            </Form.Item>
            <Form.Item name="expires_at" label="到期时间">
              <DatePicker showTime className="w-full" />
            </Form.Item>
            <Form.Item name="issue_key" label="开通时下发 Key" valuePropName="checked">
              <Switch />
            </Form.Item>
            <Form.Item name="key_alias" label="Key alias">
              <Input maxLength={255} placeholder="可选" />
            </Form.Item>
            <Form.Item name="metadata" label="metadata">
              <Input.TextArea rows={5} spellCheck={false} />
            </Form.Item>
          </Form>
        </Space>
      </Drawer>

      <Drawer
        title={selectedSubscription ? `订阅详情 ${selectedSubscription.subscription_id}` : "订阅详情"}
        open={detailDrawerOpen}
        width={980}
        destroyOnClose
        onClose={closeDetailDrawer}
        extra={
          selectedSubscription ? (
            <Button
              icon={<ReloadOutlined />}
              onClick={() => void loadSubscriptionDetail(selectedSubscription.subscription_id)}
              loading={detailLoading}
            >
              刷新
            </Button>
          ) : null
        }
      >
        {selectedSubscription ? (
          <Space direction="vertical" size={16} className="w-full">
            <SubscriptionSummary subscription={selectedSubscription} currentPlan={currentPlan} />

            <Descriptions size="small" bordered column={2}>
              <Descriptions.Item label="subscription_id">
                <Text code>{selectedSubscription.subscription_id}</Text>
              </Descriptions.Item>
              <Descriptions.Item label="状态">
                <SubscriptionStatusTag status={selectedSubscription.status} />
              </Descriptions.Item>
              <Descriptions.Item label="project_id">{selectedSubscription.project_id}</Descriptions.Item>
              <Descriptions.Item label="litellm_team_id">
                <Text code>{selectedSubscription.litellm_team_id ?? "-"}</Text>
              </Descriptions.Item>
              <Descriptions.Item label="plan_id">
                <Text code>{selectedSubscription.plan_id}</Text>
              </Descriptions.Item>
              <Descriptions.Item label="版本">v{selectedSubscription.version}</Descriptions.Item>
              <Descriptions.Item label="created_at">
                {formatDateTime(selectedSubscription.created_at)}
              </Descriptions.Item>
              <Descriptions.Item label="expires_at">
                {formatDateTime(selectedSubscription.expires_at)}
              </Descriptions.Item>
              <Descriptions.Item label="renewed_at">
                {formatDateTime(selectedSubscription.renewed_at)}
              </Descriptions.Item>
              <Descriptions.Item label="paused_at">{formatDateTime(selectedSubscription.paused_at)}</Descriptions.Item>
              <Descriptions.Item label="canceled_at">
                {formatDateTime(selectedSubscription.canceled_at)}
              </Descriptions.Item>
              <Descriptions.Item label="Key 数">{keyUsageLabel(selectedSubscription)}</Descriptions.Item>
            </Descriptions>

            <Card className="rounded-lg border border-slate-200 shadow-sm" bodyStyle={{ padding: 16 }}>
              <Space direction="vertical" size={12} className="w-full">
                <div className="flex items-center justify-between gap-3">
                  <Title level={4} className="!mb-0 !text-base">
                    额度余量
                  </Title>
                  <Tag color="blue">usage-ledger 推算</Tag>
                </div>
                <QuotaEstimateView
                  subscription={selectedSubscription}
                  usageEstimate={usageEstimate}
                  loading={usageLoading}
                />
              </Space>
            </Card>

            <Card className="rounded-lg border border-slate-200 shadow-sm" bodyStyle={{ padding: 16 }}>
              <Space direction="vertical" size={12} className="w-full">
                <div className="flex items-center justify-between gap-3">
                  <Title level={4} className="!mb-0 !text-base">
                    plan_snapshot
                  </Title>
                  <Tag icon={<CheckCircleOutlined />}>订阅事实源</Tag>
                </div>
                <PlanSnapshotDiff subscription={selectedSubscription} currentPlan={currentPlan} />
              </Space>
            </Card>

            <Card className="rounded-lg border border-slate-200 shadow-sm" bodyStyle={{ padding: 16 }}>
              <Space direction="vertical" size={16} className="w-full">
                <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
                  <div>
                    <Title level={4} className="!mb-1 !text-base">
                      生命周期动作
                    </Title>
                    <Text type="secondary">危险操作均会携带版本号校验，避免覆盖他人刚刚做的变更。</Text>
                  </div>
                  <Space wrap>
                    <Button
                      icon={<PauseCircleOutlined />}
                      disabled={!canPauseSelectedSubscription}
                      title={canPauseSelectedSubscription ? undefined : simpleActionDisabledText.pause}
                      loading={operationKey === "pause"}
                      onClick={() => runSimpleAction("pause")}
                    >
                      暂停
                    </Button>
                    <Button
                      danger
                      icon={<CloseCircleOutlined />}
                      disabled={!canCancelSelectedSubscription}
                      title={canCancelSelectedSubscription ? undefined : simpleActionDisabledText.cancel}
                      loading={operationKey === "cancel"}
                      onClick={() => runSimpleAction("cancel")}
                    >
                      取消
                    </Button>
                    <Button
                      danger
                      icon={<DeleteOutlined />}
                      disabled={!canExpireSelectedSubscription}
                      title={canExpireSelectedSubscription ? undefined : simpleActionDisabledText.expire}
                      loading={operationKey === "expire"}
                      onClick={() => runSimpleAction("expire")}
                    >
                      过期
                    </Button>
                  </Space>
                </div>

                <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
                  <div className="rounded-lg border border-slate-200 bg-white p-4">
                    <Title level={5} className="!mt-0">
                      续期
                    </Title>
                    <Form<RenewSubscriptionFormValues>
                      form={renewForm}
                      layout="vertical"
                      disabled={!canRenewSelectedSubscription}
                    >
                      <Form.Item name="expires_at" label="新的到期时间">
                        <DatePicker showTime className="w-full" />
                      </Form.Item>
                      <Form.Item name="issue_key" label="续期同时下发 Key" valuePropName="checked">
                        <Switch />
                      </Form.Item>
                      <Form.Item name="key_alias" label="Key alias">
                        <Input maxLength={255} placeholder="可选" />
                      </Form.Item>
                      <Form.Item name="metadata" label="metadata">
                        <Input.TextArea rows={3} spellCheck={false} />
                      </Form.Item>
                    </Form>
                    <Button
                      type="primary"
                      icon={<SyncOutlined />}
                      disabled={!canRenewSelectedSubscription}
                      loading={operationKey === "renew"}
                      onClick={submitRenewSubscription}
                    >
                      提交续期
                    </Button>
                  </div>

                  <div className="rounded-lg border border-slate-200 bg-white p-4">
                    <Title level={5} className="!mt-0">
                      升降级
                    </Title>
                    <Alert type="info" showIcon className="mb-3" message="升级即时生效；降级当前周期结束后生效" />
                    <Form<UpgradeSubscriptionFormValues>
                      form={upgradeForm}
                      layout="vertical"
                      disabled={!canUpgradeSelectedSubscription}
                    >
                      <Form.Item
                        name="plan_id"
                        label="目标 Plan"
                        rules={[
                          { required: true, message: "请选择目标 active Plan" },
                          () => ({
                            validator(_, value) {
                              if (!value || value !== selectedSubscription.plan_id) return Promise.resolve();
                              return Promise.reject(new Error("请选择不同的 Plan"));
                            },
                          }),
                        ]}
                      >
                        <Select
                          showSearch
                          optionFilterProp="label"
                          options={activePlanSelectOptions}
                          loading={loadingPlans}
                          placeholder="仅可选择 active Plan"
                        />
                      </Form.Item>
                      <Form.Item name="expires_at" label="新的到期时间">
                        <DatePicker showTime className="w-full" />
                      </Form.Item>
                      <Form.Item name="issue_key" label="升降级同时下发 Key" valuePropName="checked">
                        <Switch />
                      </Form.Item>
                      <Form.Item name="key_alias" label="Key alias">
                        <Input maxLength={255} placeholder="可选" />
                      </Form.Item>
                      <Form.Item name="metadata" label="metadata">
                        <Input.TextArea rows={3} spellCheck={false} />
                      </Form.Item>
                    </Form>
                    <Button
                      type="primary"
                      disabled={!canUpgradeSelectedSubscription}
                      loading={operationKey === "upgrade"}
                      onClick={submitUpgradeSubscription}
                    >
                      提交升降级
                    </Button>
                  </div>
                </div>
              </Space>
            </Card>

            <Card className="rounded-lg border border-slate-200 shadow-sm" bodyStyle={{ padding: 16 }}>
              <Space direction="vertical" size={16} className="w-full">
                <div className="flex flex-col gap-2 md:flex-row md:items-center md:justify-between">
                  <div>
                    <Title level={4} className="!mb-1 !text-base">
                      Key 管理
                    </Title>
                    <Text type="secondary">同一订阅多 Key 共享额度，增删/轮换不重置 5h 或周窗口。</Text>
                  </div>
                  <Tag color={keyCount(selectedSubscription) >= selectedSubscription.plan_snapshot.max_keys ? "warning" : "processing"}>
                    {keyUsageLabel(selectedSubscription)}
                  </Tag>
                </div>
                <Descriptions size="small" bordered column={1}>
                  <Descriptions.Item label="当前 Key IDs">
                    {selectedSubscription.litellm_key_ids.length ? (
                      <Space wrap>
                        {selectedSubscription.litellm_key_ids.map((keyId) => (
                          <Tag key={keyId}>
                            <Text code>{keyId}</Text>
                          </Tag>
                        ))}
                      </Space>
                    ) : (
                      "-"
                    )}
                  </Descriptions.Item>
                </Descriptions>

                {canIssueSelectedKey ? null : (
                  <Alert
                    type="warning"
                    showIcon
                    message={
                      selectedSubscription.status !== "active"
                        ? "仅 active 订阅可代发 Key"
                        : "当前 Key 数已达到 Plan 上限"
                    }
                  />
                )}

                <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
                  <div className="rounded-lg border border-slate-200 bg-white p-4">
                    <Title level={5} className="!mt-0">
                      代发 Key
                    </Title>
                    <Form<IssueKeyFormValues>
                      form={issueKeyForm}
                      layout="vertical"
                      initialValues={{ metadata: defaultMetadataJson }}
                      disabled={!canIssueSelectedKey}
                    >
                      <Form.Item name="key_alias" label="Key alias">
                        <Input maxLength={255} placeholder="可选" />
                      </Form.Item>
                      <Form.Item name="metadata" label="metadata">
                        <Input.TextArea rows={3} spellCheck={false} />
                      </Form.Item>
                    </Form>
                    <Button
                      type="primary"
                      icon={<KeyOutlined />}
                      disabled={!canIssueSelectedKey}
                      loading={operationKey === "issue-key"}
                      onClick={submitIssueKey}
                    >
                      代发 Key
                    </Button>
                  </div>

                  <div className="rounded-lg border border-slate-200 bg-white p-4">
                    <Title level={5} className="!mt-0">
                      吊销 Key
                    </Title>
                    <Form<RevokeKeyFormValues> form={revokeKeyForm} layout="vertical">
                      <Form.Item name="key_id" label="Key ID" rules={[{ required: true, message: "请选择 Key ID" }]}>
                        <Select
                          showSearch
                          optionFilterProp="label"
                          disabled={!selectedSubscription.litellm_key_ids.length}
                          options={selectedSubscription.litellm_key_ids.map((keyId) => ({
                            label: keyId,
                            value: keyId,
                          }))}
                          placeholder="选择要吊销的 Key"
                        />
                      </Form.Item>
                    </Form>
                    <Button
                      danger
                      icon={<DeleteOutlined />}
                      disabled={!selectedSubscription.litellm_key_ids.length}
                      loading={operationKey === "revoke-key"}
                      onClick={submitRevokeKey}
                    >
                      吊销 Key
                    </Button>
                  </div>
                </div>
              </Space>
            </Card>
          </Space>
        ) : (
          emptyDetailDrawerContent
        )}
      </Drawer>

      <ProvisionResultModal result={provisionResult} onClose={() => setProvisionResult(null)} />
    </Space>
  );
}

export function CodePlanSubscriptionsPage({ accessToken, userRole }: CodePlanSubscriptionsPageProps) {
  if (!userRole || !isProxyAdminRole(userRole)) {
    return (
      <CodePlanSubscriptionsFrame>
        <CodePlanErrorState message="需要 Proxy Admin 权限" />
      </CodePlanSubscriptionsFrame>
    );
  }

  return (
    <CodePlanSubscriptionsFrame>
      <CodePlanSubscriptionsManager accessToken={accessToken} />
    </CodePlanSubscriptionsFrame>
  );
}
