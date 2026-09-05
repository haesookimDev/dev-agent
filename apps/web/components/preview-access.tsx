"use client";

import { useEffect, useRef, useState } from "react";
import type { MessageCatalog } from "../i18n/types";
import { apiJSON, browserApi, BrowserAPIError } from "../lib/browser-api";
import { previewExchangeURL, type PreviewLaunch } from "../lib/preview";
import { Icon } from "./icon";

type Availability = { available: true; expires_at: string } |
  { available: false; reason: "not_configured" | "unavailable" };

export function PreviewAccess({ workId, revision, messages }: {
  workId: string; revision: string; messages: MessageCatalog["preview"];
}) {
  const [availability, setAvailability] = useState<Availability | null>(null);
  const [checking, setChecking] = useState(true);
  const [opening, setOpening] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [retry, setRetry] = useState(0);
  const inFlight = useRef(false);
  const endpoint = `${browserApi}/api/work-items/${workId}`;

  useEffect(() => {
    const controller = new AbortController();
    async function check() {
      try {
        const value = await apiJSON<Availability>(`${endpoint}/preview-access`, {
          signal: AbortSignal.any([controller.signal, AbortSignal.timeout(10_000)]),
        });
        if (!controller.signal.aborted) setAvailability(value.available && Date.parse(value.expires_at) <= Date.now()
          ? { available: false, reason: "unavailable" } : value);
      } catch {
        if (!controller.signal.aborted) setAvailability(null);
      } finally {
        if (!controller.signal.aborted) setChecking(false);
      }
    }
    void check();
    const interval = setInterval(() => void check(), 15_000);
    return () => { controller.abort(); clearInterval(interval); };
  }, [endpoint, revision, retry]);

  async function open() {
    if (inFlight.current) return;
    setError(""); setNotice("");
    // Reserve a tab within the click gesture, before the asynchronous grant request.
    const tab = window.open("about:blank", "_blank");
    if (!tab) { setError(messages.popupBlocked); return; }
    tab.opener = null;
    tab.document.title = messages.opening;
    tab.document.body.textContent = messages.opening;
    inFlight.current = true; setOpening(true);
    try {
      const launch = await apiJSON<PreviewLaunch>(`${endpoint}/preview-grants`, {
        method: "POST", signal: AbortSignal.timeout(10_000),
      });
      const url = previewExchangeURL(launch, workId);
      if (tab.closed) throw new Error("Preview tab closed");
      // The blank document inherits the dashboard origin; the code is only a POST body.
      const form = tab.document.createElement("form");
      form.method = "POST"; form.action = url;
      const code = tab.document.createElement("input");
      code.type = "hidden"; code.name = "code"; code.value = launch.launch_code;
      form.append(code); tab.document.body.append(form);
      form.submit(); form.remove();
      setNotice(messages.opened);
    } catch (cause) {
      tab.close();
      setError(cause instanceof BrowserAPIError && [401, 403, 404].includes(cause.status)
        ? messages.permissionError : messages.openError);
      setRetry((value) => value + 1);
    } finally {
      inFlight.current = false; setOpening(false);
    }
  }

  if (availability?.available === false && availability.reason === "not_configured") return null;
  const ready = availability?.available;
  return <section className="previewCard" aria-labelledby="preview-title">
    <div className="previewHeading"><Icon name="shield" /><h3 id="preview-title">{messages.title}</h3></div>
    <p>{messages.description}</p>
    {ready ? <>
      <button className="primaryButton" type="button" disabled={opening} onClick={open}>
        {opening ? messages.opening : messages.open}<span aria-hidden="true">↗</span>
      </button>
      <p className="previewHint">{messages.expiryHint}</p>
    </> : <p role="status">{checking ? messages.checking : availability ? messages.unavailable : messages.checkError}</p>}
    {!ready && !checking && <button className="secondaryButton" type="button" onClick={() => {
      setChecking(true); setRetry((value) => value + 1);
    }}><Icon name="refresh" />{messages.retry}</button>}
    {error && <p className="formError" role="alert">{error}</p>}
    {notice && <p className="actionNotice" role="status">{notice}</p>}
  </section>;
}
