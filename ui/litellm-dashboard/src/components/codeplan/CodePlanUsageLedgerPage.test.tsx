import { describe, expect, it } from "vitest";

import {
  isUsageLedgerTruncated,
  summarizeUsageLedgerEntries,
  usageLedgerMetadataReason,
} from "./CodePlanUsageLedgerPage";
import type { CodePlanUsageLedgerEntry, UsageLedgerEventType } from "./types";

const entry = (
  overrides: Partial<CodePlanUsageLedgerEntry> & {
    event_type?: UsageLedgerEventType;
    metadata?: CodePlanUsageLedgerEntry["metadata"];
  } = {},
): CodePlanUsageLedgerEntry => ({
  event_id: overrides.event_id ?? "evt-1",
  request_id: overrides.request_id ?? "req-1",
  subscription_id: overrides.subscription_id ?? "sub-1",
  event_type: overrides.event_type ?? "manual_adjust",
  credits: overrides.credits ?? 1,
  input_tokens: 0,
  output_tokens: 0,
  cache_read_tokens: 0,
  cache_write_tokens: 0,
  input_multiplier: 1,
  output_multiplier: 1,
  cache_read_multiplier: 0,
  cache_write_multiplier: 0,
  rule_version: 3,
  metadata: overrides.metadata ?? {},
});

describe("CodePlanUsageLedgerPage helpers", () => {
  it("extracts a trimmed manual adjustment reason from metadata", () => {
    expect(usageLedgerMetadataReason({ reason: " support credit " })).toBe("support credit");
    expect(usageLedgerMetadataReason({ reason: 42 })).toBe("-");
  });

  it("marks the table as truncated at the API limit", () => {
    expect(isUsageLedgerTruncated(Array.from({ length: 999 }, () => entry()))).toBe(false);
    expect(isUsageLedgerTruncated(Array.from({ length: 1000 }, () => entry()))).toBe(true);
  });

  it("summarizes billable credits using settle + manual_adjust only", () => {
    const reserveOverrides = {
      event_id: "1",
      request_id: "r1",
      event_type: "reserve" as const,
      credits: 40,
    };
    const settleOverrides = {
      event_id: "2",
      request_id: "r1",
      event_type: "settle" as const,
      credits: -25,
    };
    const adjustOverrides = {
      event_id: "3",
      request_id: "r2",
      event_type: "manual_adjust" as const,
      credits: 10,
    };
    const refundOverrides = {
      event_id: "4",
      request_id: "r3",
      event_type: "refund" as const,
      credits: 5,
    };
    const totals = summarizeUsageLedgerEntries([
      entry(reserveOverrides),
      entry(settleOverrides),
      entry(adjustOverrides),
      entry(refundOverrides),
    ]);

    expect(totals.eventCount).toBe(4);
    expect(totals.requestCount).toBe(3);
    expect(totals.billableEventCount).toBe(2);
    expect(totals.credits).toBe(-15);
    expect(totals.usd).toBeCloseTo(-0.15);
  });

  it("can summarize all event credits when billableOnly is false", () => {
    const reserveOverrides = { event_type: "reserve" as const, credits: 40 };
    const settleOverrides = { event_type: "settle" as const, credits: -25 };
    const totals = summarizeUsageLedgerEntries([entry(reserveOverrides), entry(settleOverrides)], {
      billableOnly: false,
    });

    expect(totals.credits).toBe(15);
    expect(totals.billableEventCount).toBe(2);
  });
});
