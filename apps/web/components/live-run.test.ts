import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { getMessages } from "../i18n";
import type { WorkItem } from "../lib/types";
import { LiveRun } from "./live-run";

const work: WorkItem = {
  id: "progress-test", correlation_id: "progress-correlation", source: "web", source_external_id: null,
  title: "Progress test", requirement: "Do not confuse stopped work with completion", repository: "demo/progress",
  status: "queued", version: 1, requested_by: "test-user", assigned_worker_id: null,
  budget_minutes: 240, budget_cost: null, replan_limit: 3, approval_required: true,
  github_installation_id: null, github_issue_number: null, pull_request_url: null,
  created_at: "2026-09-06T00:00:00Z", updated_at: "2026-09-06T00:00:00Z",
};

describe.each(["ko", "en"] as const)("%s work progress", (locale) => {
  it.each(["failed", "cancelled"] as const)("does not render a completion percentage for %s", (status) => {
    const html = renderToStaticMarkup(createElement(LiveRun, {
      initialWork: { ...work, status }, initialEvents: [], initialArtifacts: [], locale, messages: getMessages(locale),
    }));
    expect(html).toContain(getMessages(locale).status[status]);
    expect(html).not.toContain('class="progress"');
    expect(html).not.toContain("100%");
    expect(html).toContain(locale === "ko" ? "작업이 완료되지 않았습니다" : "This work did not complete");
  });

  it.each([["queued", 0], ["completed", 100]] as const)("retains the existing %s percentage", (status, progress) => {
    const html = renderToStaticMarkup(createElement(LiveRun, {
      initialWork: { ...work, status }, initialEvents: [], initialArtifacts: [], locale, messages: getMessages(locale),
    }));
    expect(html).toContain(`<strong>${progress}%</strong>`);
    expect(html).toContain('class="progress"');
    expect(html).not.toContain('class="stoppedProgress"');
  });
});
