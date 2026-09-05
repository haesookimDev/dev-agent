import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, expect, it, vi } from "vitest";
import { LocalTime } from "./local-time";

afterEach(() => vi.restoreAllMocks());

it.each(["ko", "en"] as const)("renders locale-independent UTC time during %s hydration", (locale) => {
  // Server and browser ICU data need not agree on localized day-period labels.
  vi.spyOn(Date.prototype, "toLocaleTimeString").mockReturnValue("PM 2:03:03");
  const markup = renderToStaticMarkup(createElement(LocalTime, {
    value: "2026-09-05T14:03:03.123456", locale, timeOnly: true,
  }));
  expect(markup).toContain('dateTime="2026-09-05T14:03:03.123456Z"');
  expect(markup).toContain(">14:03:03 UTC</time>");
  expect(Date.prototype.toLocaleTimeString).not.toHaveBeenCalled();
});

it("uses the UTC instant for the initial full date without server locale formatting", () => {
  vi.spyOn(Date.prototype, "toLocaleString").mockReturnValue("a different server locale format");
  const markup = renderToStaticMarkup(createElement(LocalTime, {
    value: "2026-09-06T00:03:03+10:00", locale: "ko",
  }));
  expect(markup).toContain('title="2026-09-05T14:03:03.000Z"');
  expect(markup).toContain(">2026-09-05 14:03 UTC</time>");
  expect(Date.prototype.toLocaleString).not.toHaveBeenCalled();
});
