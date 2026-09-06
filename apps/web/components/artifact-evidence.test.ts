import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { getMessages } from "../i18n";
import { ArtifactEvidence, artifactSize } from "./artifact-evidence";

describe.each(["ko", "en"] as const)("%s artifact evidence", (locale) => {
  it("shows an honest empty state without file controls", () => {
    const messages = getMessages(locale).artifacts;
    const html = renderToStaticMarkup(createElement(ArtifactEvidence, { workId: "work", artifacts: [], locale, messages }));
    expect(html).toContain(messages.empty);
    expect(html).not.toContain("<button");
    expect(html).not.toContain("<dialog");
  });
  it("escapes filenames and preserves separately labeled original links", () => {
    const messages = getMessages(locale).artifacts;
    const html = renderToStaticMarkup(createElement(ArtifactEvidence, { workId: "work/one", locale, messages,
      artifacts: [{ id: "file?x=1", work_item_id: "work/one", kind: "evidence", name: '<script> 한글 ✅.txt',
        content_type: "text/plain", size_bytes: 34, created_at: "2026-09-06T00:00:00Z" }],
    }));
    expect(html).toContain("&lt;script&gt; 한글 ✅.txt");
    expect(html).not.toContain("<script>");
    expect(html).toContain(`${messages.open}: &lt;script&gt;`);
    expect(html).toContain(`${messages.original}: &lt;script&gt;`);
    expect(html).toContain('aria-haspopup="dialog"');
    expect(html).toContain("work%2Fone/artifacts/file%3Fx%3D1");
    expect(html).toContain("34 B");
    expect(html).not.toContain("<dialog");
  });
  it("formats actual byte units and invalid retained sizes", () => {
    expect(artifactSize(0, locale)).toBe("0 B");
    expect(artifactSize(1536, locale)).toBe("1.5 KiB");
    expect(artifactSize(10 * 1024 * 1024, locale)).toBe("10 MiB");
    expect(artifactSize(-1, locale)).toBe("—");
    expect(artifactSize(NaN, locale)).toBe("—");
  });
  it("retains expired metadata without misleading preview or original controls", () => {
    const messages = getMessages(locale).artifacts;
    const html = renderToStaticMarkup(createElement(ArtifactEvidence, { workId: "work", locale, messages,
      artifacts: [{ id: "expired", work_item_id: "work", kind: "evidence", name: "retained <file>.txt",
        content_type: "text/plain", size_bytes: 34, created_at: "2026-08-01T00:00:00Z",
        expired_at: "2026-09-06T00:00:00Z" }],
    }));
    expect(html).toContain(messages.expired);
    expect(html).toContain(messages.errors.expired);
    expect(html).toContain("retained &lt;file&gt;.txt");
    expect(html).toContain("34 B");
    expect(html).toContain('data-expired="true"');
    expect(html).not.toContain("<button");
    expect(html).not.toContain('href=');
    expect(html).not.toContain("<dialog");
  });
});
