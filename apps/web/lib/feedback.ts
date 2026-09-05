import type { WorkStatus } from "./types";

const feedbackStatuses = new Set<WorkStatus>([
  "queued", "provisioning", "analyzing", "implementing", "verifying",
  "awaiting_feedback", "awaiting_approval", "awaiting_input", "budget_exhausted",
]);

export function canSendFeedback(status: WorkStatus): boolean {
  return feedbackStatuses.has(status);
}
