"use client";

import {
  Alert,
  Button,
  Card,
  Drawer,
  Empty,
  Form,
  Input,
  InputNumber,
  Modal,
  Segmented,
  Select,
  Skeleton,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from "antd";
import { CheckCircleOutlined, EditOutlined, PlusOutlined, ReloadOutlined, StopOutlined } from "@ant-design/icons";
import type { PropsWithChildren } from "react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { isProxyAdminRole } from "@/utils/roles";
import {
  CODEPLAN_ADMIN_ENDPOINTS,
  activateCodePlanPlan,
  archiveCodePlanPlan,
  createCodePlanPlan,
  formatCodePlanError,
  isCodePlanVersionConflict,
  listCodePlanAvailableModels,
  listCodePlanCreditRules,
  listCodePlanPlans,
  listCodePlanSubscriptions,
  updateCodePlanPlan,
} from "./codeplan_networking";
import type { CodePlanCreditRule, CodePlanPlan, PlanStatus } from "./types";
import {
  createPlanPayloadFromValues,
  defaultCodePlanPlanFormValues,
  modelNameFromRecord,
  patchPlanPayloadFromValues,
  planToFormValues,
  type CodePlanPlanFormValues,
} from "./plan_form_utils";

const { Text, Title } = Typography;

interface CodePlanSection {
  title: string;
  description: string;
  endpoint: string;
  nextSlice: string;
}

interface CodePlanPageProps {
  section: CodePlanSection;
  userRole?: string | null;
}

interface CodePlanRouteProps {
  accessToken?: string | null;
  userRole?: string | null;
}

type StatusFilter = PlanStatus | "all";
type DrawerMode = "create" | "edit";
type PlanAction = "activate" | "archive";

const statusLabels: Record<PlanStatus, string> = {
  draft: "Draft",
  active: "Active",
  archived: "Archived",
};

const statusColors: Record<PlanStatus, string> = {
  draft: "blue",
  active: "green",
  archived: "default",
};

export const CODE_PLAN_PAGE_DEFINITIONS = {
  plans: {
    title: "套餐管理",
    description: "管理 Code Plan 套餐、credits 配额、allowed_models 和版本状态。",
    endpoint: CODEPLAN_ADMIN_ENDPOINTS.plans,
    nextSlice: "ADM-02",
  },
  creditRules: {
    title: "计费规则",
    description: "管理 credits multiplier、USD 映射和规则版本状态。",
    endpoint: CODEPLAN_ADMIN_ENDPOINTS.creditRules,
    nextSlice: "ADM-03",
  },
  subscriptions: {
    title: "订阅管理",
    description: "管理项目订阅、plan_snapshot、状态机动作和 LiteLLM key 操作入口。",
    endpoint: CODEPLAN_ADMIN_ENDPOINTS.subscriptions,
    nextSlice: "ADM-04",
  },
  usageLedger: {
    title: "用量账本",
    description: "查看 usage ledger 事件、credits 汇总和 manual-adjust 操作入口。",
    endpoint: CODEPLAN_ADMIN_ENDPOINTS.usageLedger,
    nextSlice: "ADM-05",
  },
} as const;

const formatDateTime = (value?: string | null): string => {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
};

export function CodePlanBreadcrumb({ title }: { title: string }) {
  return (
    <nav className="flex items-center gap-2 text-sm text-slate-500" aria-label="breadcrumb">
      <span>Code Plan</span>
      <span>/</span>
      <span className="font-medium text-slate-900">{title}</span>
    </nav>
  );
}

export function CodePlanLoadingState() {
  return (
    <Card className="rounded-lg border border-slate-200 shadow-sm">
      <Skeleton active paragraph={{ rows: 4 }} />
    </Card>
  );
}

export function CodePlanEmptyState({ title, description }: { title: string; description: string }) {
  return (
    <div className="flex min-h-[220px] items-center justify-center rounded-lg border border-dashed border-slate-200 bg-slate-50 px-4 py-8">
      <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={<span className="text-slate-600">{title}</span>}>
        <Text className="mx-auto max-w-md text-center text-slate-500">{description}</Text>
      </Empty>
    </div>
  );
}

export function CodePlanErrorState({ error, message }: { error?: unknown; message?: string }) {
  return <Alert type="error" showIcon message={message ?? formatCodePlanError(error)} />;
}

function CodePlanPageFrame({ section, children }: PropsWithChildren<{ section: CodePlanSection }>) {
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

function StatusTag({ status }: { status: PlanStatus }) {
  return <Tag color={statusColors[status]}>{statusLabels[status]}</Tag>;
}

function CodePlanPlansManager({ accessToken }: { accessToken?: string | null }) {
  const [form] = Form.useForm<CodePlanPlanFormValues>();
  const [messageApi, messageContextHolder] = message.useMessage();
  const [modalApi, modalContextHolder] = Modal.useModal();
  const [plans, setPlans] = useState<CodePlanPlan[]>([]);
  const [creditRules, setCreditRules] = useState<CodePlanCreditRule[]>([]);
  const [modelNames, setModelNames] = useState<string[]>([]);
  const [subscriptionCounts, setSubscriptionCounts] = useState<Record<string, number>>({});
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const [loadingPlans, setLoadingPlans] = useState(false);
  const [loadingReferences, setLoadingReferences] = useState(false);
  const [loadingSubscriptionCounts, setLoadingSubscriptionCounts] = useState(false);
  const [plansError, setPlansError] = useState<string | null>(null);
  const [referenceError, setReferenceError] = useState<string | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [drawerMode, setDrawerMode] = useState<DrawerMode>("create");
  const [editingPlan, setEditingPlan] = useState<CodePlanPlan | null>(null);
  const [savingPlan, setSavingPlan] = useState(false);
  const [actionPlanId, setActionPlanId] = useState<string | null>(null);

  const modelOptions = useMemo(() => modelNames.map((name) => ({ label: name, value: name })), [modelNames]);

  const creditRuleOptions = useMemo(
    () =>
      creditRules.map((rule) => ({
        label: `${rule.name} (${rule.credit_rule_id})`,
        value: rule.credit_rule_id,
      })),
    [creditRules],
  );

  const fetchSubscriptionCounts = useCallback(
    async (nextPlans: CodePlanPlan[]) => {
      if (!accessToken || nextPlans.length === 0) {
        setSubscriptionCounts({});
        return;
      }
      setLoadingSubscriptionCounts(true);
      try {
        const entries = await Promise.all(
          nextPlans.map(async (plan) => {
            const response = await listCodePlanSubscriptions(accessToken, { plan_id: plan.plan_id });
            return [plan.plan_id, response.data?.length ?? 0] as const;
          }),
        );
        setSubscriptionCounts(Object.fromEntries(entries));
      } catch (error) {
        messageApi.warning(formatCodePlanError(error));
      } finally {
        setLoadingSubscriptionCounts(false);
      }
    },
    [accessToken, messageApi],
  );

  const loadPlans = useCallback(async () => {
    if (!accessToken) return;
    setLoadingPlans(true);
    setPlansError(null);
    try {
      const response = await listCodePlanPlans(
        accessToken,
        statusFilter === "all" ? undefined : { status: statusFilter },
      );
      const nextPlans = response.data ?? [];
      setPlans(nextPlans);
      await fetchSubscriptionCounts(nextPlans);
    } catch (error) {
      setPlansError(formatCodePlanError(error));
    } finally {
      setLoadingPlans(false);
    }
  }, [accessToken, fetchSubscriptionCounts, statusFilter]);

  const loadReferences = useCallback(async () => {
    if (!accessToken) return;
    setLoadingReferences(true);
    setReferenceError(null);
    try {
      const [rulesResponse, modelsResponse] = await Promise.all([
        listCodePlanCreditRules(accessToken, { status: "active" }),
        listCodePlanAvailableModels(accessToken),
      ]);
      const nextRules = rulesResponse.data ?? [];
      const nextModels = Array.from(
        new Set((modelsResponse.data ?? []).map(modelNameFromRecord).filter((name): name is string => Boolean(name))),
      ).sort((a, b) => a.localeCompare(b));
      setCreditRules(nextRules);
      setModelNames(nextModels);
    } catch (error) {
      setReferenceError(formatCodePlanError(error));
    } finally {
      setLoadingReferences(false);
    }
  }, [accessToken]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      void loadPlans();
    }, 0);
    return () => window.clearTimeout(timer);
  }, [loadPlans]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      void loadReferences();
    }, 0);
    return () => window.clearTimeout(timer);
  }, [loadReferences]);

  const openCreateDrawer = useCallback(() => {
    setDrawerMode("create");
    setEditingPlan(null);
    form.setFieldsValue(defaultCodePlanPlanFormValues);
    setDrawerOpen(true);
  }, [form]);

  const openEditDrawer = useCallback(
    (plan: CodePlanPlan) => {
      setDrawerMode("edit");
      setEditingPlan(plan);
      form.setFieldsValue(planToFormValues(plan));
      setDrawerOpen(true);
    },
    [form],
  );

  const closeDrawer = useCallback(() => {
    setDrawerOpen(false);
    setEditingPlan(null);
    form.resetFields();
  }, [form]);

  const handleError = useCallback(
    async (error: unknown) => {
      const text = formatCodePlanError(error);
      messageApi.error(text);
      if (isCodePlanVersionConflict(error)) {
        await loadPlans();
      }
    },
    [loadPlans, messageApi],
  );

  const handleSubmit = async () => {
    let values: CodePlanPlanFormValues;
    try {
      values = await form.validateFields();
    } catch {
      return;
    }

    setSavingPlan(true);
    try {
      if (drawerMode === "create") {
        const payload = createPlanPayloadFromValues(values);
        await createCodePlanPlan(accessToken, payload);
        messageApi.success("套餐已创建");
      } else if (editingPlan) {
        const payload = patchPlanPayloadFromValues(values, editingPlan.version);
        await updateCodePlanPlan(accessToken, editingPlan.plan_id, payload);
        messageApi.success("套餐已更新");
      }
      closeDrawer();
      await loadPlans();
    } catch (error) {
      if (error instanceof SyntaxError || (error instanceof Error && error.message.includes("metadata"))) {
        form.setFields([{ name: "metadata", errors: ["metadata 必须是 JSON object"] }]);
      } else {
        await handleError(error);
      }
    } finally {
      setSavingPlan(false);
    }
  };

  const handlePlanAction = useCallback(
    (plan: CodePlanPlan, action: PlanAction) => {
      if (action === "activate" && (plan.allowed_models?.length ?? 0) === 0) {
        messageApi.error("激活套餐前至少选择一个 allowed_model");
        return;
      }

      const activating = action === "activate";
      const title = activating ? "激活套餐？" : "归档套餐？";
      const okText = activating ? "激活" : "归档";
      const content = (
        <Space direction="vertical" size={8}>
          <Text>{activating ? "套餐激活后可以被新订阅使用。" : "归档后不可复活，也不能继续编辑。"}</Text>
          <Text type="secondary">修改 active Plan 仅影响新订阅，老订阅继续使用 plan_snapshot。</Text>
          <Text type="secondary">本次操作会带上当前版本号 v{plan.version} 做乐观锁校验。</Text>
        </Space>
      );
      const onOk = async () => {
        setActionPlanId(`${action}:${plan.plan_id}`);
        try {
          if (activating) {
            await activateCodePlanPlan(accessToken, plan.plan_id, plan.version);
            messageApi.success("套餐已激活");
          } else {
            await archiveCodePlanPlan(accessToken, plan.plan_id, plan.version);
            messageApi.success("套餐已归档");
          }
          await loadPlans();
        } catch (error) {
          await handleError(error);
          throw error;
        } finally {
          setActionPlanId(null);
        }
      };
      const confirmOptions = {
        title,
        okText,
        cancelText: "取消",
        okButtonProps: { danger: !activating },
        content,
        onOk,
      };
      modalApi.confirm(confirmOptions);
    },
    [accessToken, handleError, loadPlans, messageApi, modalApi],
  );

  const columns = useMemo(
    () => [
      {
        title: "名称",
        dataIndex: "name",
        key: "name",
        width: 220,
        render: (_: unknown, plan: CodePlanPlan) => (
          <Space direction="vertical" size={0}>
            <Text strong>{plan.name}</Text>
            {plan.description ? (
              <Text type="secondary" className="max-w-[220px]" ellipsis={{ tooltip: plan.description }}>
                {plan.description}
              </Text>
            ) : null}
          </Space>
        ),
      },
      {
        title: "Slug",
        dataIndex: "plan_id",
        key: "plan_id",
        width: 190,
        render: (planId: string) => <Text code>{planId}</Text>,
      },
      {
        title: "状态",
        dataIndex: "status",
        key: "status",
        width: 110,
        render: (status: PlanStatus) => <StatusTag status={status} />,
      },
      {
        title: "5h 配额",
        dataIndex: "quota_5h",
        key: "quota_5h",
        width: 110,
      },
      {
        title: "周配额",
        dataIndex: "quota_weekly",
        key: "quota_weekly",
        width: 110,
      },
      {
        title: "模型数",
        dataIndex: "allowed_models",
        key: "allowed_models",
        width: 110,
        render: (models: string[]) => (
          <Tooltip title={models?.length ? models.join(", ") : "未配置模型"}>
            <Tag color={models?.length ? "processing" : "warning"}>{models?.length ?? 0}</Tag>
          </Tooltip>
        ),
      },
      {
        title: "max_keys",
        dataIndex: "max_keys",
        key: "max_keys",
        width: 110,
      },
      {
        title: "订阅引用",
        key: "subscription_count",
        width: 120,
        render: (_: unknown, plan: CodePlanPlan) => (
          <Text type={loadingSubscriptionCounts ? "secondary" : undefined}>
            {loadingSubscriptionCounts && subscriptionCounts[plan.plan_id] === undefined
              ? "加载中"
              : subscriptionCounts[plan.plan_id] ?? 0}
          </Text>
        ),
      },
      {
        title: "版本",
        dataIndex: "version",
        key: "version",
        width: 90,
        render: (version: number) => <Text>v{version}</Text>,
      },
      {
        title: "更新时间",
        dataIndex: "updated_at",
        key: "updated_at",
        width: 190,
        render: (value?: string | null) => <Text>{formatDateTime(value)}</Text>,
      },
      {
        title: "操作",
        key: "actions",
        fixed: "right" as const,
        width: 220,
        render: (_: unknown, plan: CodePlanPlan) => {
          const cannotEdit = plan.status === "archived";
          const canActivate = plan.status === "draft";
          const canArchive = plan.status === "active";
          const missingModels = (plan.allowed_models?.length ?? 0) === 0;

          return (
            <Space size={8}>
              <Tooltip title={cannotEdit ? "archived Plan 不可编辑" : "编辑"}>
                <Button
                  size="small"
                  icon={<EditOutlined />}
                  disabled={cannotEdit}
                  onClick={() => openEditDrawer(plan)}
                />
              </Tooltip>
              {canActivate ? (
                <Tooltip title={missingModels ? "激活前至少选择一个 allowed_model" : "激活"}>
                  <Button
                    size="small"
                    type="primary"
                    icon={<CheckCircleOutlined />}
                    disabled={missingModels}
                    loading={actionPlanId === `activate:${plan.plan_id}`}
                    onClick={() => handlePlanAction(plan, "activate")}
                  />
                </Tooltip>
              ) : null}
              {canArchive ? (
                <Tooltip title="归档">
                  <Button
                    size="small"
                    danger
                    icon={<StopOutlined />}
                    loading={actionPlanId === `archive:${plan.plan_id}`}
                    onClick={() => handlePlanAction(plan, "archive")}
                  />
                </Tooltip>
              ) : null}
            </Space>
          );
        },
      },
    ],
    [actionPlanId, handlePlanAction, loadingSubscriptionCounts, openEditDrawer, subscriptionCounts],
  );

  return (
    <Space direction="vertical" size={16} className="w-full">
      {messageContextHolder}
      {modalContextHolder}
      {plansError ? <Alert type="error" showIcon message={plansError} /> : null}
      {referenceError ? <Alert type="warning" showIcon message={referenceError} /> : null}
      <Card className="rounded-lg border border-slate-200 shadow-sm" bodyStyle={{ padding: 16 }}>
        <div className="mb-4 flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
          <Segmented
            value={statusFilter}
            onChange={(value) => setStatusFilter(value as StatusFilter)}
            options={[
              { label: "全部", value: "all" },
              { label: "Draft", value: "draft" },
              { label: "Active", value: "active" },
              { label: "Archived", value: "archived" },
            ]}
          />
          <Space wrap>
            <Button icon={<ReloadOutlined />} onClick={loadPlans} loading={loadingPlans}>
              刷新
            </Button>
            <Button type="primary" icon={<PlusOutlined />} onClick={openCreateDrawer}>
              新建套餐
            </Button>
            <Text type="secondary">共 {plans.length} 个套餐</Text>
          </Space>
        </div>
        <Table<CodePlanPlan>
          rowKey="plan_id"
          columns={columns}
          dataSource={plans}
          loading={loadingPlans}
          scroll={{ x: 1380 }}
          pagination={{ pageSize: 10, showSizeChanger: true }}
          locale={{
            emptyText: (
              <CodePlanEmptyState title="暂无套餐" description="创建 draft 套餐后可在这里配置模型、配额和状态。" />
            ),
          }}
        />
      </Card>
      <Drawer
        title={drawerMode === "create" ? "新建套餐" : `编辑套餐 ${editingPlan?.plan_id ?? ""}`}
        open={drawerOpen}
        width={560}
        destroyOnClose
        onClose={closeDrawer}
        footer={
          <div className="flex justify-end gap-2">
            <Button onClick={closeDrawer}>取消</Button>
            <Button type="primary" loading={savingPlan} onClick={handleSubmit}>
              {drawerMode === "create" ? "创建" : "保存"}
            </Button>
          </div>
        }
      >
        <Space direction="vertical" size={16} className="w-full">
          {editingPlan?.status === "active" ? (
            <Alert type="info" showIcon message="修改 active Plan 仅影响新订阅，老订阅继续使用 plan_snapshot。" />
          ) : null}
          {editingPlan?.status === "archived" ? (
            <Alert type="warning" showIcon message="archived Plan 不可复活，也不可继续编辑。" />
          ) : null}
          <Form<CodePlanPlanFormValues>
            form={form}
            layout="vertical"
            initialValues={defaultCodePlanPlanFormValues}
            disabled={savingPlan || editingPlan?.status === "archived"}
          >
            {editingPlan ? (
              <Form.Item label="Slug">
                <Input value={editingPlan.plan_id} disabled />
              </Form.Item>
            ) : null}
            <Form.Item name="name" label="名称" rules={[{ required: true, message: "请输入套餐名称" }]}>
              <Input maxLength={120} placeholder="Code Plan Pro" />
            </Form.Item>
            <Form.Item name="description" label="描述">
              <Input.TextArea rows={3} maxLength={500} placeholder="内部管理备注" />
            </Form.Item>
            <div className="grid grid-cols-1 gap-x-3 md:grid-cols-2">
              <Form.Item
                name="quota_5h"
                label="5h credits 配额"
                rules={[{ required: true, type: "number", min: 1, message: "quota_5h 必须大于 0" }]}
              >
                <InputNumber className="w-full" min={1} precision={0} />
              </Form.Item>
              <Form.Item
                name="quota_weekly"
                label="周 credits 配额"
                dependencies={["quota_5h"]}
                rules={[
                  { required: true, type: "number", min: 1, message: "quota_weekly 必须大于 0" },
                  ({ getFieldValue }) => ({
                    validator(_, value) {
                      const quota5h = getFieldValue("quota_5h");
                      if (typeof value !== "number" || typeof quota5h !== "number" || value >= quota5h) {
                        return Promise.resolve();
                      }
                      return Promise.reject(new Error("quota_weekly 必须大于等于 quota_5h"));
                    },
                  }),
                ]}
              >
                <InputNumber className="w-full" min={1} precision={0} />
              </Form.Item>
              <Form.Item
                name="max_keys"
                label="max_keys"
                rules={[{ required: true, type: "number", min: 1, message: "max_keys 必须大于等于 1" }]}
              >
                <InputNumber className="w-full" min={1} precision={0} />
              </Form.Item>
              <Form.Item name="default_max_output_tokens" label="默认 max output tokens">
                <InputNumber className="w-full" min={1} precision={0} />
              </Form.Item>
              <Form.Item name="rpm_limit" label="RPM 限制">
                <InputNumber className="w-full" min={1} precision={0} />
              </Form.Item>
              <Form.Item name="tpm_limit" label="TPM 限制">
                <InputNumber className="w-full" min={1} precision={0} />
              </Form.Item>
              <Form.Item name="max_parallel_requests" label="最大并发请求">
                <InputNumber className="w-full" min={1} precision={0} />
              </Form.Item>
              <Form.Item name="credit_rule_id" label="计费规则">
                <Select
                  allowClear
                  showSearch
                  optionFilterProp="label"
                  options={creditRuleOptions}
                  loading={loadingReferences}
                  placeholder="选择 active credit rule"
                />
              </Form.Item>
            </div>
            <Form.Item name="allowed_models" label="allowed_models">
              <Select
                mode="multiple"
                showSearch
                optionFilterProp="label"
                options={modelOptions}
                loading={loadingReferences}
                placeholder="从 /v1/models 选择模型"
              />
            </Form.Item>
            <Form.Item name="metadata" label="metadata">
              <Input.TextArea rows={5} spellCheck={false} />
            </Form.Item>
          </Form>
          <Text type="secondary">
            保存编辑时会提交当前版本号 v{editingPlan?.version ?? 1}，后端返回 409 时请刷新后重试。
          </Text>
        </Space>
      </Drawer>
    </Space>
  );
}

export function CodePlanPage({ section, userRole }: CodePlanPageProps) {
  if (!userRole || !isProxyAdminRole(userRole)) {
    return (
      <CodePlanPageFrame section={section}>
        <CodePlanErrorState message="需要 Proxy Admin 权限" />
      </CodePlanPageFrame>
    );
  }

  return (
    <CodePlanPageFrame section={section}>
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,2fr)_minmax(280px,1fr)]">
        <Card className="rounded-lg border border-slate-200 shadow-sm">
          <div className="mb-5 flex flex-col gap-1">
            <Title level={4} className="!mb-0 !text-base">
              {section.title}
            </Title>
            <Text>业务表格和操作将在 {section.nextSlice} 接入。</Text>
          </div>
          <CodePlanEmptyState
            title={`${section.title} 暂无数据`}
            description="当前切片只挂载管理面骨架、统一状态组件和后端 API 封装。"
          />
        </Card>
        <Card className="rounded-lg border border-slate-200 shadow-sm">
          <div className="flex flex-col gap-4">
            <div>
              <Text>REST endpoint</Text>
              <p className="mt-1 break-all rounded-md bg-slate-50 px-3 py-2 font-mono text-sm text-slate-900">
                {section.endpoint}
              </p>
            </div>
            <div>
              <Text>权限</Text>
              <p className="mt-1 text-sm text-slate-700">Proxy Admin only</p>
            </div>
            <div>
              <Text>错误文案</Text>
              <div className="mt-2 flex flex-col gap-2">
                <Alert type="warning" showIcon message="需要 Proxy Admin 权限" />
                <Alert type="error" showIcon message="数据库未连接" />
              </div>
            </div>
          </div>
        </Card>
      </div>
    </CodePlanPageFrame>
  );
}

export function CodePlanPlansPage({ accessToken, userRole }: CodePlanRouteProps) {
  if (!userRole || !isProxyAdminRole(userRole)) {
    return (
      <CodePlanPageFrame section={CODE_PLAN_PAGE_DEFINITIONS.plans}>
        <CodePlanErrorState message="需要 Proxy Admin 权限" />
      </CodePlanPageFrame>
    );
  }

  return (
    <CodePlanPageFrame section={CODE_PLAN_PAGE_DEFINITIONS.plans}>
      <CodePlanPlansManager accessToken={accessToken} />
    </CodePlanPageFrame>
  );
}

export function CodePlanCreditRulesPage({ userRole }: CodePlanRouteProps) {
  return <CodePlanPage section={CODE_PLAN_PAGE_DEFINITIONS.creditRules} userRole={userRole} />;
}

export function CodePlanSubscriptionsPage({ userRole }: CodePlanRouteProps) {
  return <CodePlanPage section={CODE_PLAN_PAGE_DEFINITIONS.subscriptions} userRole={userRole} />;
}

export function CodePlanUsageLedgerPage({ userRole }: CodePlanRouteProps) {
  return <CodePlanPage section={CODE_PLAN_PAGE_DEFINITIONS.usageLedger} userRole={userRole} />;
}
