import { describe, expect, it } from "vitest";
import {
  createPlanPayloadFromValues,
  modelNameFromRecord,
  parseCodePlanMetadata,
  patchPlanPayloadFromValues,
  planToFormValues,
  type CodePlanPlanFormValues,
} from "./plan_form_utils";
import type { CodePlanPlan } from "./types";

const baseValues: CodePlanPlanFormValues = {
  name: " Pro Plan ",
  description: " ",
  quota_5h: 100,
  quota_weekly: 500,
  allowed_models: ["gpt-4o"],
  rpm_limit: 60,
  tpm_limit: null,
  max_parallel_requests: 3,
  max_keys: 2,
  default_max_output_tokens: null,
  credit_rule_id: "standard-credit",
  metadata: '{"tier":"pro"}',
};

const expectedCreatePayload = {
  name: "Pro Plan",
  description: null,
  quota_5h: 100,
  quota_weekly: 500,
  allowed_models: ["gpt-4o"],
  rpm_limit: 60,
  tpm_limit: null,
  max_parallel_requests: 3,
  max_keys: 2,
  default_max_output_tokens: null,
  credit_rule_id: "standard-credit",
  metadata: { tier: "pro" },
};

describe("plan_form_utils", () => {
  it("creates a trimmed plan payload with null optional fields", () => {
    expect(createPlanPayloadFromValues(baseValues)).toEqual(expectedCreatePayload);
  });

  it("allows draft plan payloads without allowed models", () => {
    const values = { ...baseValues, allowed_models: [] };

    expect(createPlanPayloadFromValues(values).allowed_models).toEqual([]);
    expect(patchPlanPayloadFromValues(values, 7).allowed_models).toEqual([]);
  });

  it("adds the optimistic lock version to edit payloads", () => {
    expect(patchPlanPayloadFromValues(baseValues, 7)).toMatchObject({
      version: 7,
      name: "Pro Plan",
      metadata: { tier: "pro" },
    });
  });

  it("rejects metadata that is not a JSON object", () => {
    expect(() => parseCodePlanMetadata("[]")).toThrow("metadata 必须是 JSON object");
  });

  it("maps persisted plans back to drawer form values", () => {
    const plan: CodePlanPlan = {
      plan_id: "pro-plan",
      name: "Pro Plan",
      description: null,
      status: "active",
      version: 3,
      quota_5h: 100,
      quota_weekly: 500,
      allowed_models: ["gpt-4o"],
      rpm_limit: null,
      tpm_limit: 1000,
      max_parallel_requests: null,
      max_keys: 2,
      default_max_output_tokens: null,
      credit_rule_id: null,
      metadata: { tier: "pro" },
    };

    const values = planToFormValues(plan);

    expect(values.allowed_models).toEqual(["gpt-4o"]);
    expect(JSON.parse(values.metadata ?? "{}")).toEqual({ tier: "pro" });
  });

  it("uses proxy model_name before OpenAI-style id fallback", () => {
    expect(modelNameFromRecord({ id: "fallback", model_name: "gpt-4o" })).toBe("gpt-4o");
    expect(modelNameFromRecord({ id: "fallback" })).toBe("fallback");
    expect(modelNameFromRecord({ id: " ", model_name: " " })).toBeNull();
  });
});
