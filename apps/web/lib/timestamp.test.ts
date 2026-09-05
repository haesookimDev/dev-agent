import { expect, it } from "vitest";
import { timestampWithZone } from "./timestamp";

it("interprets SQLite timestamps as UTC while preserving PostgreSQL offsets", () => {
  expect(timestampWithZone("2026-09-05T09:00:00.123456")).toBe("2026-09-05T09:00:00.123456Z");
  expect(timestampWithZone("2026-09-05T09:00:00Z")).toBe("2026-09-05T09:00:00Z");
  expect(timestampWithZone("2026-09-05T18:00:00+09:00")).toBe("2026-09-05T18:00:00+09:00");
  expect(new Date(timestampWithZone("2026-09-05T09:00:00")).toISOString()).toBe("2026-09-05T09:00:00.000Z");
});
