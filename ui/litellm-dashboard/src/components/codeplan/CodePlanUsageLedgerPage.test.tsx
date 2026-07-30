import { describe, expect, it } from "vitest";

import { isUsageLedgerTruncated, usageLedgerMetadataReason } from "./CodePlanUsageLedgerPage";
import type { CodePlanUsageLedgerEntry } from "./types";

const entry = (metadata: CodePlanUsageLedgerEntry["metadata"] = {}): CodePlanUsageLedgerEntry => ({
  event_id: "evt-1",
  request_id: "req-1",
  subscription_id: "sub-1",
  event_type: "manual_adjust",
  credits: 1,
  input_tokens: 0,
  output_tokens: 0,
  cache_read_tokens: 0,
  cache_write_tokens: 0,
  input_multiplier: 1,
  output_multiplier: 1,
  cache_read_multiplier: 0,
  cache_write_multiplier: 0,
  rule_version: 3,
  metadata,
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
});
