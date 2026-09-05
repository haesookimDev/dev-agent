import { describe, expect, it } from "vitest";
import { filterWorkItems, matchesFilter } from "./work-list";
import type { WorkItem, WorkStatus } from "./types";

const work = (status: WorkStatus, title = "검증 작업") => ({
  id: `work-${status}`, repository: "Acme/Service", title, status,
}) as WorkItem;

describe("work list", () => {
  it("searches title, repository and ID without case or surrounding-space sensitivity", () => {
    const items = [work("queued"), work("completed", "완료")];
    expect(filterWorkItems(items, " ACME/service ", "all")).toEqual(items);
    expect(filterWorkItems(items, "검증", "all")).toEqual([items[0]]);
    expect(filterWorkItems(items, "work-completed", "all")).toEqual([items[1]]);
    expect(filterWorkItems(items, "missing", "all")).toEqual([]);
  });
  it("keeps failed work in attention but excludes terminal work from active", () => {
    expect(matchesFilter(work("failed"), "attention")).toBe(true);
    for (const status of ["failed", "cancelled", "completed"] as const) {
      expect(matchesFilter(work(status), "active")).toBe(false);
    }
    expect(matchesFilter(work("awaiting_approval"), "attention")).toBe(true);
    expect(matchesFilter(work("awaiting_approval"), "active")).toBe(true);
    expect(matchesFilter(work("pr_created"), "completed")).toBe(false);
  });
  it("combines search with status and handles empty data", () => {
    const items = [work("queued"), work("completed")];
    expect(filterWorkItems(items, "Acme", "completed")).toEqual([items[1]]);
    expect(filterWorkItems([], "", "all")).toEqual([]);
  });
});
