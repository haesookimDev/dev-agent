import { describe, expect, it } from "vitest";
import { defaultLocale, getMessages, isLocale, localeTag, locales } from ".";

describe("i18n catalogs", () => {
  it("supports Korean and English with Korean as the default", () => {
    expect(locales).toEqual(["ko", "en"]);
    expect(defaultLocale).toBe("ko");
    expect(isLocale("ko")).toBe(true);
    expect(isLocale("en")).toBe(true);
    expect(isLocale("ja")).toBe(false);
  });

  it("returns localized labels and date tags", () => {
    expect(getMessages("ko").status.awaiting_approval).toBe("승인 대기");
    expect(getMessages("en").status.awaiting_approval).toBe("Awaiting approval");
    expect(localeTag("ko")).toBe("ko-KR");
    expect(localeTag("en")).toBe("en-US");
  });
});
