import { isAttentionStatus } from "./status";
import type { WorkItem } from "./types";

export type WorkFilter = "all" | "active" | "attention" | "completed";

export function matchesFilter(item: WorkItem, filter: WorkFilter): boolean {
  if (filter === "attention") return isAttentionStatus(item.status);
  if (filter === "completed") return item.status === "completed";
  if (filter === "active") return !["completed", "failed", "cancelled"].includes(item.status);
  return true;
}

export function filterWorkItems(items: WorkItem[], query: string, filter: WorkFilter): WorkItem[] {
  const normalized = query.trim().toLowerCase();
  return items.filter((item) => matchesFilter(item, filter) &&
    `${item.title} ${item.repository} ${item.id}`.toLowerCase().includes(normalized));
}
