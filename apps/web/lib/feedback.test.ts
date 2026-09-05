import { expect, it } from "vitest";
import { canSendFeedback } from "./feedback";
import type { WorkStatus } from "./types";

const expectations: Record<WorkStatus, boolean> = {
  queued: true, provisioning: true, analyzing: true, implementing: true, verifying: true,
  awaiting_feedback: true, awaiting_approval: true, awaiting_input: true, budget_exhausted: true,
  committing: false, pr_created: false, completed: false, failed: false, cancelled: false,
};

it.each(Object.entries(expectations))("feedback availability for %s is %s", (status, expected) => {
  expect(canSendFeedback(status as WorkStatus)).toBe(expected);
});

it("does not enable feedback for an unrecognized server status", () => {
  expect(canSendFeedback("future_state" as WorkStatus)).toBe(false);
});
