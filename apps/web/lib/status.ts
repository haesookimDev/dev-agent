import type { WorkStatus } from "./types";

const steps: WorkStatus[] = [
  "queued",
  "provisioning",
  "analyzing",
  "implementing",
  "verifying",
  "awaiting_approval",
  "committing",
  "pr_created",
  "completed",
];

export function statusProgress(status: WorkStatus): number {
  if (status === "completed") return 100;
  if (["failed", "cancelled"].includes(status)) return 100;
  if (["awaiting_feedback", "awaiting_input", "budget_exhausted"].includes(status)) return 62;
  const index = steps.indexOf(status);
  return index < 0 ? 0 : Math.round((index / (steps.length - 1)) * 100);
}
export function statusLabel(
  status: WorkStatus,
  labels?: Record<WorkStatus, string>,
): string {
  return labels?.[status] ?? status.replaceAll("_", " ");
}

export function isAttentionStatus(status: WorkStatus): boolean {
  return ["awaiting_approval", "awaiting_feedback", "awaiting_input", "budget_exhausted", "failed"].includes(status);
}
