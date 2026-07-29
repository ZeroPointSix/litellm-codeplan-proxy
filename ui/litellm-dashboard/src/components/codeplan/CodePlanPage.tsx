"use client";

import { Alert, Card, Empty, Skeleton, Typography } from "antd";
import type { PropsWithChildren } from "react";
import { isProxyAdminRole } from "@/utils/roles";
import { CODEPLAN_ADMIN_ENDPOINTS, formatCodePlanError } from "./codeplan_networking";

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
  userRole?: string | null;
}

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
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-5 p-4 lg:p-6">
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

export function CodePlanPlansPage({ userRole }: CodePlanRouteProps) {
  return <CodePlanPage section={CODE_PLAN_PAGE_DEFINITIONS.plans} userRole={userRole} />;
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
