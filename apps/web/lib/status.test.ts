import { describe, expect, it } from "vitest";
import { getMessages } from "../i18n";
import { isAttentionStatus, statusLabel, statusProgress } from "./status";

describe("status helpers", () => {
  it("maps the normal delivery path monotonically", () => {
    const path = ["queued", "analyzing", "verifying", "committing", "completed"] as const;
    const values = path.map(statusProgress);
    expect(values).not.toContain(null);
    expect(values).toEqual([...values].sort((a, b) => Number(a) - Number(b)));
    expect(values.at(-1)).toBe(100);
  });

  it("marks human intervention states", () => {
    expect(isAttentionStatus("awaiting_approval")).toBe(true);
    expect(isAttentionStatus("implementing")).toBe(false);
  });

  it.each(["failed", "cancelled"] as const)("does not claim completion for %s work", (status) => {
    expect(statusProgress(status)).toBeNull();
  });

  it("uses localized status labels", () => {
    expect(statusLabel("verifying", getMessages("ko").status)).toBe("검증 중");
    expect(statusLabel("verifying", getMessages("en").status)).toBe("Verifying");
  });
});
