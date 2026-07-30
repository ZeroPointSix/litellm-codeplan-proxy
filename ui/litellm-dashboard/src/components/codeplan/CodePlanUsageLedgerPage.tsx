"use client";

import type { TableProps } from "antd";
import {
  Alert,
  Button,
  Card,
  DatePicker,
  Form,
  Input,
  InputNumber,
  Modal,
  Select,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from "antd";
import { DownloadOutlined, PlusOutlined, ReloadOutlined } from "@ant-design/icons";
// ZER-267 acceptance requires the Code Plan usage ledger aggregation to use Tremor AreaChart.
// eslint-disable-next-line no-restricted-imports
import { AreaChart } from "@tremor/react";
import dayjs, { type Dayjs } from "dayjs";
import { useCallback, useEffect, useMemo, useRef, useState, type Key, type ReactNode } from "react";
import { isProxyAdminRole } from "@/utils/roles";
import {
  formatCodePlanError,
  listCodePlanUsageLedger,
  manualAdjustCodePlanUsageLedger,
  summarizeCodePlanUsageLedger,
} from "./codeplan_networking";
import { CODE_PLAN_PAGE_DEFINITIONS, CodePlanBreadcrumb, CodePlanEmptyState, CodePlanErrorState } from "./CodePlanPage";
import type {
  CodePlanUsageLedgerAggregate,
  CodePlanUsageLedgerEntry,
  UsageLedgerEventType,
  UsageLedgerGroupBy,
} from "./types";

const { RangePicker } = DatePicker;
const { Text, Title } = Typography;

const MAX_LEDGER_LIMIT = 1000;
const CREDIT_USD_RATE = 0.01;
/** Align with backend summary default: settle + manual_adjust are billable. */
export const BILLABLE_USAGE_LEDGER_EVENT_TYPES = new Set<UsageLedgerEventType>(["settle", "manual_adjust"]);
const ledgerSection = CODE_PLAN_PAGE_DEFINITIONS.usageLedger;

interface CodePlanUsageLedgerPageProps {
  accessToken?: string | null;
  userRole?: string | null;
}

interface FilterValues {
  time_range?: [Dayjs, Dayjs] | null;
  subscription_id?: string;
  project_id?: string;
  user_id?: string;
  model?: string;
  event_type?: UsageLedgerEventType | "all";
}

interface ManualAdjustFormValues {
  subscription_id?: string;
  request_id?: string;
  credits?: number;
  reason?: string;
  project_id?: string;
  user_id?: string;
  model?: string;
}

const eventTypes: UsageLedgerEventType[] = [
  "reserve",
  "settle",
  "release",
  "refund",
  "manual_adjust",
  "compensate",
  "expire",
  "pending_usage",
];

const eventColors: Record<UsageLedgerEventType, string> = {
  reserve: "blue",
  settle: "green",
  release: "default",
  refund: "purple",
  manual_adjust: "gold",
  compensate: "cyan",
  expire: "red",
  pending_usage: "orange",
};

const groupByOptions: { label: string; value: UsageLedgerGroupBy }[] = [
  { label: "按天", value: "day" },
  { label: "按小时", value: "hour" },
  { label: "按模型", value: "model" },
  { label: "按用户", value: "user" },
];

const trimToNull = (value?: string | null): string | null => {
  const normalized = value?.trim();
  return normalized ? normalized : null;
};

const formatDateTime = (value?: string | null): string => {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
};

const formatNumber = (value?: number | null, maximumFractionDigits = 4): string => {
  if (value === null || value === undefined || Number.isNaN(value)) return "-";
  return new Intl.NumberFormat(undefined, { maximumFractionDigits }).format(value);
};

const formatCredits = (value?: number | null): string => formatNumber(value, 4);
const creditsToUsd = (credits?: number | null): number => (credits ?? 0) * CREDIT_USD_RATE;
const formatUsd = (value?: number | null): string =>
  new Intl.NumberFormat(undefined, { style: "currency", currency: "USD", maximumFractionDigits: 4 }).format(value ?? 0);

export const usageLedgerMetadataReason = (metadata?: CodePlanUsageLedgerEntry["metadata"] | null): string => {
  const reason = metadata?.reason;
  return typeof reason === "string" && reason.trim() ? reason.trim() : "-";
};

export const isUsageLedgerTruncated = (entries: CodePlanUsageLedgerEntry[]): boolean =>
  entries.length >= MAX_LEDGER_LIMIT;

export const summarizeUsageLedgerEntries = (
  entries: CodePlanUsageLedgerEntry[],
  options?: { billableOnly?: boolean },
): { credits: number; usd: number; requestCount: number; eventCount: number; billableEventCount: number } => {
  const billableOnly = options?.billableOnly ?? true;
  const creditEntries = billableOnly
    ? entries.filter((entry) => BILLABLE_USAGE_LEDGER_EVENT_TYPES.has(entry.event_type))
    : entries;
  const credits = creditEntries.reduce((sum, entry) => sum + (entry.credits ?? 0), 0);
  return {
    credits,
    usd: creditsToUsd(credits),
    requestCount: new Set(entries.map((entry) => entry.request_id)).size,
    eventCount: entries.length,
    billableEventCount: creditEntries.length,
  };
};

const signedNumber = (value?: number | null): string => {
  if (value === null || value === undefined || Number.isNaN(value)) return "-";
  const formatted = formatCredits(Math.abs(value));
  if (value > 0) return `+${formatted}`;
  if (value < 0) return `-${formatted}`;
  return formatted;
};

const csvEscape = (value: unknown): string => {
  const text = value === null || value === undefined ? "" : String(value);
  return `"${text.replace(/"/g, '""')}"`;
};

const downloadCsv = (entries: CodePlanUsageLedgerEntry[]): void => {
  const headers = [
    "created_at",
    "event_id",
    "request_id",
    "subscription_id",
    "project_id",
    "user_id",
    "model",
    "event_type",
    "credits",
    "usd_estimate",
    "rule_version",
    "reason",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "input_multiplier",
    "output_multiplier",
    "cache_read_multiplier",
    "cache_write_multiplier",
  ];
  const rows = entries.map((entry) =>
    [
      entry.created_at,
      entry.event_id,
      entry.request_id,
      entry.subscription_id,
      entry.project_id,
      entry.user_id,
      entry.model,
      entry.event_type,
      entry.credits,
      creditsToUsd(entry.credits),
      entry.rule_version,
      usageLedgerMetadataReason(entry.metadata),
      entry.input_tokens,
      entry.output_tokens,
      entry.cache_read_tokens,
      entry.cache_write_tokens,
      entry.input_multiplier,
      entry.output_multiplier,
      entry.cache_read_multiplier,
      entry.cache_write_multiplier,
    ]
      .map(csvEscape)
      .join(","),
  );
  const blob = new Blob([[headers.join(","), ...rows].join("\n")], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `codeplan-usage-ledger-${dayjs().format("YYYYMMDD-HHmmss")}.csv`;
  link.click();
  URL.revokeObjectURL(url);
};

const queryFromFilters = (values: FilterValues, groupBy?: UsageLedgerGroupBy) => {
  const [start, end] = values.time_range ?? [];
  return {
    limit: MAX_LEDGER_LIMIT,
    group_by: groupBy,
    start_time: start?.toISOString() ?? null,
    end_time: end?.toISOString() ?? null,
    subscription_id: trimToNull(values.subscription_id),
    project_id: trimToNull(values.project_id),
    user_id: trimToNull(values.user_id),
    model: trimToNull(values.model),
    event_type: values.event_type && values.event_type !== "all" ? values.event_type : null,
  };
};

function CodePlanUsageLedgerFrame({ children }: { children: ReactNode }) {
  return (
    <div className="mx-auto flex w-full max-w-7xl flex-col gap-5 p-4 lg:p-6">
      <CodePlanBreadcrumb title={ledgerSection.title} />
      <div className="flex flex-col gap-1">
        <Title level={2} className="!mb-0">
          {ledgerSection.title}
        </Title>
        <Text>{ledgerSection.description}</Text>
      </div>
      {children}
    </div>
  );
}

function EventTag({ eventType }: { eventType: UsageLedgerEventType }) {
  return <Tag color={eventColors[eventType]}>{eventType}</Tag>;
}

function CodePlanUsageLedgerManager({ accessToken }: { accessToken?: string | null }) {
  const [filterForm] = Form.useForm<FilterValues>();
  const [manualForm] = Form.useForm<ManualAdjustFormValues>();
  const didInitializeFilters = useRef(false);
  const [messageApi, messageContextHolder] = message.useMessage();
  const [modalApi, modalContextHolder] = Modal.useModal();
  const [entries, setEntries] = useState<CodePlanUsageLedgerEntry[]>([]);
  const [summary, setSummary] = useState<CodePlanUsageLedgerAggregate[]>([]);
  const [chainRows, setChainRows] = useState<Record<string, CodePlanUsageLedgerEntry[]>>({});
  const [expandedKeys, setExpandedKeys] = useState<Key[]>([]);
  const [groupBy, setGroupBy] = useState<UsageLedgerGroupBy>("day");
  const [loading, setLoading] = useState(false);
  const [chainLoadingKey, setChainLoadingKey] = useState<string | null>(null);
  const [adjustOpen, setAdjustOpen] = useState(false);
  const [savingAdjust, setSavingAdjust] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const currentFilters = useCallback(() => filterForm.getFieldsValue(), [filterForm]);

  const load = useCallback(async () => {
    if (!accessToken) return;
    setLoading(true);
    setError(null);
    try {
      const filters = currentFilters();
      const [ledgerResponse, summaryResponse] = await Promise.all([
        listCodePlanUsageLedger(accessToken, queryFromFilters(filters)),
        summarizeCodePlanUsageLedger(accessToken, queryFromFilters(filters, groupBy)),
      ]);
      setEntries(ledgerResponse.data ?? []);
      setSummary(summaryResponse.data ?? []);
      setExpandedKeys([]);
      setChainRows({});
    } catch (err) {
      setError(formatCodePlanError(err));
    } finally {
      setLoading(false);
    }
  }, [accessToken, currentFilters, groupBy]);

  useEffect(() => {
    if (!didInitializeFilters.current) {
      didInitializeFilters.current = true;
      filterForm.setFieldsValue({
        time_range: [dayjs().subtract(7, "day"), dayjs()],
        event_type: "all",
      });
    }
    const timer = window.setTimeout(() => {
      void load();
    }, 0);
    return () => window.clearTimeout(timer);
  }, [filterForm, load]);

  const totals = useMemo(() => summarizeUsageLedgerEntries(entries, { billableOnly: true }), [entries]);

  const chartData = useMemo(
    () =>
      [...summary].reverse().map((row) => ({
        group: row.group || "-",
        Credits: Number(row.credits.toFixed(4)),
        "USD estimate": Number(creditsToUsd(row.credits).toFixed(4)),
      })),
    [summary],
  );

  const loadChain = useCallback(
    async (requestId: string) => {
      if (!accessToken || chainRows[requestId]) return;
      setChainLoadingKey(requestId);
      try {
        const response = await listCodePlanUsageLedger(accessToken, { request_id: requestId, limit: MAX_LEDGER_LIMIT });
        setChainRows((current) => ({ ...current, [requestId]: response.data ?? [] }));
      } catch (err) {
        messageApi.error(formatCodePlanError(err));
      } finally {
        setChainLoadingKey(null);
      }
    },
    [accessToken, chainRows, messageApi],
  );

  const openManualAdjust = (entry?: CodePlanUsageLedgerEntry) => {
    const nextManualValues = {
      subscription_id: entry?.subscription_id,
      request_id: entry?.request_id ? `${entry.request_id}-manual` : `manual-${dayjs().format("YYYYMMDD-HHmmss")}`,
      project_id: entry?.project_id ?? undefined,
      user_id: entry?.user_id ?? undefined,
      model: entry?.model ?? undefined,
    };
    manualForm.setFieldsValue(nextManualValues);
    setAdjustOpen(true);
  };

  const submitManualAdjust = async () => {
    if (!accessToken) return;
    try {
      const values = await manualForm.validateFields();
      const credits = values.credits ?? 0;
      const manualAdjustPayload = {
        subscription_id: values.subscription_id!,
        request_id: values.request_id!,
        credits,
        reason: values.reason!,
        project_id: trimToNull(values.project_id),
        user_id: trimToNull(values.user_id),
        model: trimToNull(values.model),
      };
      const confirmOptions = {
        title: "确认人工调整 credits",
        content: `将对订阅 ${values.subscription_id} 写入 ${signedNumber(credits)} credits，原因：${values.reason}`,
        okText: "确认写入",
        cancelText: "取消",
        okButtonProps: { danger: credits < 0 },
        onOk: async () => {
          setSavingAdjust(true);
          try {
            await manualAdjustCodePlanUsageLedger(accessToken, manualAdjustPayload);
            messageApi.success("manual_adjust 已写入");
            setAdjustOpen(false);
            manualForm.resetFields();
            await load();
          } catch (err) {
            messageApi.error(formatCodePlanError(err));
          } finally {
            setSavingAdjust(false);
          }
        },
      };
      modalApi.confirm(confirmOptions);
    } catch (err) {
      if (err instanceof Error) messageApi.error(err.message);
    }
  };

  const columns: TableProps<CodePlanUsageLedgerEntry>["columns"] = [
    {
      title: "时间",
      dataIndex: "created_at",
      width: 180,
      render: formatDateTime,
    },
    {
      title: "事件",
      dataIndex: "event_type",
      width: 150,
      render: (value: UsageLedgerEventType) => <EventTag eventType={value} />,
    },
    {
      title: "Credits",
      dataIndex: "credits",
      width: 130,
      align: "right",
      render: (value: number, record) => (
        <Text strong={record.event_type === "manual_adjust"} type={value < 0 ? "danger" : undefined}>
          {signedNumber(value)}
        </Text>
      ),
    },
    {
      title: "USD estimate",
      dataIndex: "credits",
      width: 150,
      align: "right",
      render: (value: number) => formatUsd(creditsToUsd(value)),
    },
    {
      title: "rule_version",
      dataIndex: "rule_version",
      width: 120,
      align: "right",
      render: (value: number) => formatNumber(value, 0),
    },
    {
      title: "reason",
      dataIndex: "metadata",
      width: 220,
      render: (metadata: CodePlanUsageLedgerEntry["metadata"]) => usageLedgerMetadataReason(metadata),
    },
    {
      title: "request_id",
      dataIndex: "request_id",
      width: 240,
      render: (value: string) => <Text code>{value}</Text>,
    },
    {
      title: "subscription_id",
      dataIndex: "subscription_id",
      width: 220,
      render: (value: string) => <Text code>{value}</Text>,
    },
    {
      title: "project / user",
      width: 220,
      render: (_, record) => (
        <Space direction="vertical" size={0}>
          <Text>{record.project_id || "-"}</Text>
          <Text type="secondary">{record.user_id || "-"}</Text>
        </Space>
      ),
    },
    {
      title: "model",
      dataIndex: "model",
      width: 180,
      render: (value?: string | null) => value || "-",
    },
    {
      title: "tokens / 倍率",
      width: 220,
      render: (_, record) => (
        <Space direction="vertical" size={0}>
          <Text type="secondary">
            in {formatNumber(record.input_tokens, 0)} / out {formatNumber(record.output_tokens, 0)}
          </Text>
          <Text type="secondary">
            ×{formatNumber(record.input_multiplier, 2)} / ×{formatNumber(record.output_multiplier, 2)}
          </Text>
        </Space>
      ),
    },
    {
      title: "操作",
      width: 120,
      fixed: "right",
      render: (_, record) => (
        <Button size="small" onClick={() => openManualAdjust(record)}>
          调整
        </Button>
      ),
    },
  ];

  const chainColumns: TableProps<CodePlanUsageLedgerEntry>["columns"] = columns.filter(
    (column) => column.title !== "操作",
  );

  return (
    <Space direction="vertical" size={16} className="w-full">
      {messageContextHolder}
      {modalContextHolder}
      <Card className="rounded-lg border border-slate-200 shadow-sm" bodyStyle={{ padding: 16 }}>
        <Form<FilterValues> form={filterForm} layout="vertical" onFinish={() => void load()}>
          <div className="grid grid-cols-1 gap-x-3 md:grid-cols-2 xl:grid-cols-4">
            <Form.Item name="time_range" label="时间范围">
              <RangePicker showTime className="w-full" />
            </Form.Item>
            <Form.Item name="subscription_id" label="subscription_id">
              <Input allowClear placeholder="sub_..." />
            </Form.Item>
            <Form.Item name="project_id" label="project_id">
              <Input allowClear placeholder="project_..." />
            </Form.Item>
            <Form.Item name="user_id" label="user_id">
              <Input allowClear placeholder="user_..." />
            </Form.Item>
            <Form.Item name="model" label="model">
              <Input allowClear placeholder="gpt-4.1" />
            </Form.Item>
            <Form.Item name="event_type" label="event_type">
              <Select
                options={[{ label: "All", value: "all" }, ...eventTypes.map((value) => ({ label: value, value }))]}
              />
            </Form.Item>
            <Form.Item label="group_by">
              <Select value={groupBy} onChange={(value) => setGroupBy(value)} options={groupByOptions} />
            </Form.Item>
            <Form.Item label="操作">
              <Space wrap>
                <Button htmlType="submit" type="primary" icon={<ReloadOutlined />} loading={loading}>
                  查询
                </Button>
                <Button icon={<DownloadOutlined />} onClick={() => downloadCsv(entries)} disabled={!entries.length}>
                  CSV
                </Button>
                <Button icon={<PlusOutlined />} onClick={() => openManualAdjust()}>
                  人工调整
                </Button>
              </Space>
            </Form.Item>
          </div>
        </Form>
      </Card>

      {error ? <CodePlanErrorState message={error} /> : null}

      <div className="grid grid-cols-1 gap-4 md:grid-cols-4">
        <Card className="rounded-lg border border-slate-200 shadow-sm" bodyStyle={{ padding: 16 }}>
          <Text type="secondary">Events</Text>
          <Title level={3} className="!mb-0 !mt-1">
            {formatNumber(totals.eventCount, 0)}
          </Title>
        </Card>
        <Card className="rounded-lg border border-slate-200 shadow-sm" bodyStyle={{ padding: 16 }}>
          <Text type="secondary">Requests</Text>
          <Title level={3} className="!mb-0 !mt-1">
            {formatNumber(totals.requestCount, 0)}
          </Title>
        </Card>
        <Card className="rounded-lg border border-slate-200 shadow-sm" bodyStyle={{ padding: 16 }}>
          <Text type="secondary">Billable credits</Text>
          <Title level={3} className="!mb-0 !mt-1">
            {formatCredits(totals.credits)}
          </Title>
          <Text type="secondary" className="text-xs">
            settle + manual_adjust（{formatNumber(totals.billableEventCount, 0)} 条）
          </Text>
        </Card>
        <Card className="rounded-lg border border-slate-200 shadow-sm" bodyStyle={{ padding: 16 }}>
          <Text type="secondary">USD estimate</Text>
          <Title level={3} className="!mb-0 !mt-1">
            {formatUsd(totals.usd)}
          </Title>
          <Text type="secondary" className="text-xs">
            内部核算 {formatUsd(CREDIT_USD_RATE)} / credit
          </Text>
        </Card>
      </div>

      <Card className="rounded-lg border border-slate-200 shadow-sm" bodyStyle={{ padding: 16 }}>
        <div className="mb-4 flex flex-col gap-1">
          <Title level={4} className="!mb-0 !text-base">
            聚合趋势
          </Title>
          <Text type="secondary">
            默认与后端一致按 settle + manual_adjust 聚合；USD 为内部核算估算（{formatUsd(CREDIT_USD_RATE)} /
            credit），非上游真实账单。
          </Text>
        </div>
        {chartData.length ? (
          <AreaChart
            className="h-72"
            data={chartData}
            index="group"
            categories={["Credits", "USD estimate"]}
            colors={["blue", "emerald"]}
            valueFormatter={(value) => formatNumber(value, 4)}
            yAxisWidth={72}
          />
        ) : (
          <CodePlanEmptyState title="暂无聚合数据" description="调整筛选条件后可查看 credits 和 USD 趋势。" />
        )}
      </Card>

      <Card className="rounded-lg border border-slate-200 shadow-sm" bodyStyle={{ padding: 16 }}>
        <div className="mb-4 flex flex-col gap-1">
          <Title level={4} className="!mb-0 !text-base">
            流水明细
          </Title>
          <Text type="secondary">
            前端最多读取 {MAX_LEDGER_LIMIT} 条，并在表格内分页；展开行会按 request_id 拉取完整链路。
          </Text>
        </div>
        {isUsageLedgerTruncated(entries) ? (
          <Alert
            type="warning"
            showIcon
            className="mb-4"
            message={`当前结果已达到 ${MAX_LEDGER_LIMIT} 条上限，可能还有更多流水；请缩小时间或筛选条件。`}
          />
        ) : null}
        <Table<CodePlanUsageLedgerEntry>
          rowKey="event_id"
          columns={columns}
          dataSource={entries}
          loading={loading}
          scroll={{ x: 1900 }}
          rowClassName={(record) => (record.event_type === "manual_adjust" ? "bg-amber-50" : "")}
          pagination={{ pageSize: 20, showSizeChanger: true }}
          expandable={{
            expandedRowKeys: expandedKeys,
            onExpand: (expanded, record) => {
              setExpandedKeys((current) =>
                expanded ? [...current, record.event_id] : current.filter((key) => key !== record.event_id),
              );
              if (expanded) void loadChain(record.request_id);
            },
            expandedRowRender: (record) => (
              <Table<CodePlanUsageLedgerEntry>
                rowKey="event_id"
                columns={chainColumns}
                dataSource={chainRows[record.request_id] ?? []}
                loading={chainLoadingKey === record.request_id}
                pagination={false}
                size="small"
                scroll={{ x: 1660 }}
                rowClassName={(row) => (row.event_type === "manual_adjust" ? "bg-amber-50" : "")}
                locale={{ emptyText: "正在读取同 request_id 链路或暂无更多事件" }}
              />
            ),
          }}
          locale={{
            emptyText: (
              <CodePlanEmptyState title="暂无流水" description="请调整时间、订阅、项目、用户、模型或事件类型筛选。" />
            ),
          }}
        />
      </Card>

      <Modal
        title="人工调整 credits"
        open={adjustOpen}
        onCancel={() => setAdjustOpen(false)}
        onOk={submitManualAdjust}
        okText="提交"
        confirmLoading={savingAdjust}
        destroyOnClose
      >
        <Alert
          type="warning"
          showIcon
          className="mb-4"
          message="人工调整会写入 manual_adjust 账本事件，提交前会再次确认。"
        />
        <Form<ManualAdjustFormValues> form={manualForm} layout="vertical">
          <Form.Item
            name="subscription_id"
            label="subscription_id"
            rules={[{ required: true, message: "请输入 subscription_id" }]}
          >
            <Input />
          </Form.Item>
          <Form.Item name="request_id" label="request_id" rules={[{ required: true, message: "请输入 request_id" }]}>
            <Input />
          </Form.Item>
          <Form.Item
            name="credits"
            label="credits 调整值"
            rules={[
              { required: true, type: "number", message: "请输入 credits 调整值" },
              () => ({
                validator(_, value) {
                  if (typeof value === "number" && value !== 0) return Promise.resolve();
                  return Promise.reject(new Error("credits 不能为 0"));
                },
              }),
            ]}
          >
            <InputNumber className="w-full" precision={4} placeholder="正数补偿，负数扣减" />
          </Form.Item>
          <Form.Item name="reason" label="reason" rules={[{ required: true, message: "请输入调整原因" }]}>
            <Input.TextArea rows={3} maxLength={500} />
          </Form.Item>
          <div className="grid grid-cols-1 gap-x-3 md:grid-cols-2">
            <Form.Item name="project_id" label="project_id">
              <Input allowClear />
            </Form.Item>
            <Form.Item name="user_id" label="user_id">
              <Input allowClear />
            </Form.Item>
            <Form.Item name="model" label="model">
              <Input allowClear />
            </Form.Item>
          </div>
        </Form>
      </Modal>
    </Space>
  );
}

export function CodePlanUsageLedgerPage({ accessToken, userRole }: CodePlanUsageLedgerPageProps) {
  if (!userRole || !isProxyAdminRole(userRole)) {
    return (
      <CodePlanUsageLedgerFrame>
        <CodePlanErrorState message="需要 Proxy Admin 权限" />
      </CodePlanUsageLedgerFrame>
    );
  }

  return (
    <CodePlanUsageLedgerFrame>
      {!accessToken ? <Alert type="warning" showIcon message="缺少访问令牌，无法读取用量账本。" /> : null}
      <CodePlanUsageLedgerManager accessToken={accessToken} />
    </CodePlanUsageLedgerFrame>
  );
}
