import type { WorkItem } from "./types";

export function budgetMinutes(value: string): number | null {
  const minutes = Number(value);
  return Number.isInteger(minutes) && minutes >= 15 && minutes <= 1440 ? minutes : null;
}

export function budgetApproval(work: Pick<WorkItem, "status" | "version">, reviewedVersion: number | null, value: string) {
  const minutes = budgetMinutes(value);
  if (work.status !== "budget_exhausted" || reviewedVersion === null || reviewedVersion < 1
      || !Number.isSafeInteger(reviewedVersion) || reviewedVersion !== work.version || minutes === null) return null;
  return { kind: "budget" as const, decision: "approve" as const, expected_version: reviewedVersion, payload: { minutes } };
}
