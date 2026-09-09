import { describe, expect, it } from "vitest";
import { budgetApproval, budgetMinutes } from "./budget";

describe("budget confirmation", () => {
  it.each(["", " ", "invalid", "14", "1441", "-15", "15.5", "Infinity"])("rejects invalid minutes: %s", (value) => {
    expect(budgetMinutes(value)).toBeNull();
  });
  it.each(["15", "60", "1440"])("sends integer minutes: %s", (value) => {
    expect(budgetApproval({ status: "budget_exhausted", version: 4 }, 4, value)).toEqual({
      kind: "budget", decision: "approve", expected_version: 4, payload: { minutes: Number(value) },
    });
  });
  it.each([null, 3, 5, 4.5])("rejects a missing or mismatched reviewed version: %s", (version) => {
    expect(budgetApproval({ status: "budget_exhausted", version: 4 }, version, "60")).toBeNull();
  });
  it.each(["queued", "implementing", "awaiting_approval", "committing", "completed", "cancelled"] as const)("does not extend %s work", (status) => {
    expect(budgetApproval({ status, version: 4 }, 4, "60")).toBeNull();
  });
  it("does not truncate fractional input", () => {
    expect(budgetApproval({ status: "budget_exhausted", version: 4 }, 4, "60.5")).toBeNull();
  });
});
