import { describe, expect, it } from "vitest";
import { isAttentionStatus, statusProgress } from "./status";

describe("status helpers", () => {
  it("maps the normal delivery path monotonically", () => {
    const path = ["queued", "analyzing", "verifying", "committing", "completed"] as const;
    const values = path.map(statusProgress);
    expect(values).toEqual([...values].sort((a, b) => a - b));
    expect(values.at(-1)).toBe(100);
  });

  it("marks human intervention states", () => {
    expect(isAttentionStatus("awaiting_approval")).toBe(true);
    expect(isAttentionStatus("implementing")).toBe(false);
  });
});
