/* @vitest-environment jsdom */
import React from "react";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { CodePlanCreditRule, CodePlanPlan } from "@/components/codeplan/types";

const {
  listRulesMock,
  listModelsMock,
  listPlansMock,
  getRuleMock,
  updateRuleMock,
  archiveRuleMock,
} = vi.hoisted(() => ({
  listRulesMock: vi.fn(),
  listModelsMock: vi.fn(),
  listPlansMock: vi.fn(),
  getRuleMock: vi.fn(),
  updateRuleMock: vi.fn(),
  archiveRuleMock: vi.fn(),
}));

vi.mock("@/app/(dashboard)/hooks/useAuthorized", () => ({
  default: () => ({
    accessToken: "sk-test",
    userRole: "Admin",
    isAuthorized: true,
    isLoading: false,
    token: "token",
    userId: "admin",
    userEmail: "admin@example.com",
    premiumUser: true,
    disabledPersonalKeyCreation: false,
    showSSOBanner: false,
  }),
}));

vi.mock("@/components/codeplan/codeplan_networking", () => ({
  formatCodePlanError: (error: unknown) => (error instanceof Error ? error.message : String(error)),
  listCodePlanCreditRules: (...args: unknown[]) => listRulesMock(...args),
  listCodePlanAvailableModels: (...args: unknown[]) => listModelsMock(...args),
  listCodePlanPlans: (...args: unknown[]) => listPlansMock(...args),
  getCodePlanCreditRule: (...args: unknown[]) => getRuleMock(...args),
  createCodePlanCreditRule: vi.fn(),
  updateCodePlanCreditRule: (...args: unknown[]) => updateRuleMock(...args),
  activateCodePlanCreditRule: vi.fn(),
  archiveCodePlanCreditRule: (...args: unknown[]) => archiveRuleMock(...args),
}));

import CodePlanCreditRules from "./page";

const compact = (value: string | null | undefined) => (value ?? "").replace(/\s+/g, "");

const hasCompactText = (expected: string) => (_content: string, element: Element | null) => {
  if (!element) return false;
  if (compact(element.textContent) !== expected) return false;
  return Array.from(element.children).every((child) => compact(child.textContent) !== expected);
};

const baseRule = (overrides: Partial<CodePlanCreditRule>): CodePlanCreditRule => ({
  id: "id",
  credit_rule_id: "cr_demo",
  name: "Demo",
  description: "desc",
  status: "active",
  version: 1,
  input_multiplier: 1,
  output_multiplier: 2,
  cache_read_multiplier: 0.1,
  cache_write_multiplier: 0.5,
  metadata: {
    model_multipliers: [
      {
        model_name: "*",
        input_multiplier: 1,
        output_multiplier: 2,
        cache_read_multiplier: 0.1,
        cache_write_multiplier: 0.5,
      },
      {
        model_name: "gpt-4o",
        input_multiplier: 9,
        output_multiplier: 9,
        cache_read_multiplier: 9,
        cache_write_multiplier: 9,
      },
    ],
  },
  created_at: "2026-07-30T00:00:00.000Z",
  updated_at: "2026-07-30T00:00:00.000Z",
  ...overrides,
});

const fixtureRules: CodePlanCreditRule[] = [
  baseRule({
    id: "row-1",
    status: "active",
    version: 1,
    updated_at: "2026-07-30T01:00:00.000Z",
  }),
  baseRule({
    id: "row-2",
    status: "draft",
    version: 2,
    input_multiplier: 1.5,
    output_multiplier: 3,
    cache_read_multiplier: 0.2,
    cache_write_multiplier: 0.8,
    metadata: {
      model_multipliers: [
        {
          model_name: "*",
          input_multiplier: 1.5,
          output_multiplier: 3,
          cache_read_multiplier: 0.2,
          cache_write_multiplier: 0.8,
        },
        {
          model_name: "gpt-4o",
          input_multiplier: 4,
          output_multiplier: 8,
          cache_read_multiplier: 0.3,
          cache_write_multiplier: 1.2,
        },
      ],
    },
    updated_at: "2026-07-30T04:00:00.000Z",
  }),
  baseRule({
    id: "row-solo",
    credit_rule_id: "cr_solo",
    name: "Solo",
    status: "active",
    version: 1,
    input_multiplier: 1,
    output_multiplier: 1,
    cache_read_multiplier: 0,
    cache_write_multiplier: 0,
    metadata: { model_multipliers: [] },
    updated_at: "2026-07-30T03:00:00.000Z",
  }),
];

const fixturePlans: CodePlanPlan[] = [
  {
    plan_id: "plan_pro",
    name: "Pro Plan",
    status: "active",
    version: 1,
    quota_5h: 1000,
    quota_weekly: 10000,
    allowed_models: ["*"],
    max_keys: 5,
    credit_rule_id: "cr_solo",
    metadata: {},
  },
];

describe("CodePlanCreditRules page", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    listRulesMock.mockResolvedValue({ object: "list", data: fixtureRules });
    listModelsMock.mockResolvedValue({
      object: "list",
      data: [
        { id: "gpt-4o", model_name: "gpt-4o" },
        { id: "claude-sonnet-4", model_name: "claude-sonnet-4" },
      ],
    });
    listPlansMock.mockResolvedValue({ object: "list", data: fixturePlans });
    getRuleMock.mockImplementation(async (_token: string, id: string, version?: number) => {
      const match = fixtureRules.find(
        (rule) => rule.credit_rule_id === id && (version == null || rule.version === version),
      );
      if (!match) throw new Error("not found");
      return match;
    });
    window.confirm = vi.fn(() => true);
  });

  it("loads all versions then shows only absolute latest per credit_rule_id", async () => {
    render(<CodePlanCreditRules />);

    await waitFor(() => {
      expect(listRulesMock).toHaveBeenCalledWith("sk-test");
    });
    expect(listRulesMock.mock.calls[0][1]).toBeUndefined();

    await waitFor(() => {
      expect(screen.getByText(hasCompactText("共2条当前规则"))).toBeInTheDocument();
    });

    const demoRow = screen.getByText("Demo").closest("tr") as HTMLElement;
    expect(within(demoRow).getByText("v2")).toBeInTheDocument();
    expect(within(demoRow).queryByText("v1")).not.toBeInTheDocument();
    expect(screen.getByText("Demo")).toBeInTheDocument();
    expect(screen.getByText("Solo")).toBeInTheDocument();
    // Absolute latest for cr_demo is draft v2; historical active v1 is not listed.
    expect(screen.getAllByText("Demo")).toHaveLength(1);
  });

  it("Active filter hides rules whose absolute latest is draft", async () => {
    render(<CodePlanCreditRules />);
    await waitFor(() => expect(screen.getByText(hasCompactText("共2条当前规则"))).toBeInTheDocument());

    fireEvent.change(screen.getByDisplayValue("全部"), { target: { value: "active" } });

    await waitFor(() => {
      expect(screen.getByText(hasCompactText("共1条当前规则"))).toBeInTheDocument();
    });
    expect(screen.getByText("Solo")).toBeInTheDocument();
    expect(screen.queryByText("Demo")).not.toBeInTheDocument();
  });

  it("edit uses absolute latest version so PATCH cannot send stale version", async () => {
    render(<CodePlanCreditRules />);
    await waitFor(() => expect(screen.getByText("Demo")).toBeInTheDocument());

    const demoRow = screen.getByText("Demo").closest("tr") as HTMLElement;
    fireEvent.click(within(demoRow).getByText(hasCompactText("编辑")));

    await waitFor(() => expect(screen.getByText(/编辑 cr_demo/)).toBeInTheDocument());
    fireEvent.click(screen.getByText(hasCompactText("保存为新版本")));

    await waitFor(() => expect(updateRuleMock).toHaveBeenCalled());
    const [, creditRuleId, body] = updateRuleMock.mock.calls[0];
    expect(creditRuleId).toBe("cr_demo");
    expect(body.version).toBe(2);
    expect(body.input_multiplier).toBe(1.5);
  });

  it("preview credits always use * billing multipliers, not metadata model rows", async () => {
    render(<CodePlanCreditRules />);
    await waitFor(() => expect(screen.getByText("换算预览")).toBeInTheDocument());
    await waitFor(() => expect(screen.getByText("Demo")).toBeInTheDocument());

    const previewCard = screen.getByText("换算预览").closest(".ant-card") as HTMLElement;
    await waitFor(() => {
      expect(compact(previewCard.textContent)).toContain("3000credits");
    });

    const previewModelSelect = within(previewCard)
      .getAllByRole("combobox")
      .find((el) => Array.from((el as HTMLSelectElement).options).some((opt) => opt.value === "gpt-4o")) as
      | HTMLSelectElement
      | undefined;
    expect(previewModelSelect).toBeTruthy();
    fireEvent.change(previewModelSelect as HTMLSelectElement, { target: { value: "gpt-4o" } });

    await waitFor(() => {
      expect(screen.getByText(/该模型行仅作为 metadata 保存/)).toBeInTheDocument();
    });
    expect(compact(previewCard.textContent)).toContain("3000credits");
    expect(compact(previewCard.textContent)).toContain("metadata模型行");
  });

  it("blocks archive when active plans reference the rule", async () => {
    render(<CodePlanCreditRules />);
    await waitFor(() => expect(screen.getByText("Solo")).toBeInTheDocument());

    fireEvent.change(screen.getByDisplayValue("全部"), { target: { value: "active" } });
    await waitFor(() => expect(screen.getByText(hasCompactText("共1条当前规则"))).toBeInTheDocument());

    const soloRow = screen.getByText("Solo").closest("tr") as HTMLElement;
    fireEvent.click(within(soloRow).getByText(hasCompactText("归档")));

    await waitFor(() => {
      expect(screen.getByText(/不能归档：仍有 active Plan 引用/)).toBeInTheDocument();
    });
    expect(archiveRuleMock).not.toHaveBeenCalled();
  });

  it("diff loads two versions and highlights changes", async () => {
    render(<CodePlanCreditRules />);
    await waitFor(() => expect(screen.getByText("Demo")).toBeInTheDocument());

    const demoRow = screen.getByText("Demo").closest("tr") as HTMLElement;
    fireEvent.click(within(demoRow).getByText("diff"));

    await waitFor(() => expect(getRuleMock).toHaveBeenCalled());
    expect(getRuleMock).toHaveBeenCalledWith("sk-test", "cr_demo", 1);
    expect(getRuleMock).toHaveBeenCalledWith("sk-test", "cr_demo", 2);

    const diffCard = screen.getByText("版本历史 diff").closest(".ant-card") as HTMLElement;
    await waitFor(() => {
      expect(within(diffCard).getAllByText("modified").length).toBeGreaterThan(0);
    });
  });
});
