"use client";

import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import type { Locale } from "../i18n";
import type { MessageCatalog } from "../i18n/types";
import { apiJSON, browserApi, requestErrorMessage } from "../lib/browser-api";
import { statusLabel, statusProgress } from "../lib/status";
import type { AgentEvent, Artifact, WorkItem } from "../lib/types";
import { LocalTime } from "./local-time";

interface LiveRunProps {
  initialWork: WorkItem;
  initialEvents: AgentEvent[];
  initialArtifacts: Artifact[];
  locale: Locale;
  messages: MessageCatalog;
}

export function LiveRun({
  initialWork,
  initialEvents,
  initialArtifacts,
  locale,
  messages,
}: LiveRunProps) {
  const [work, setWork] = useState(initialWork);
  const [events, setEvents] = useState(initialEvents);
  const [artifacts, setArtifacts] = useState(initialArtifacts);
  const [sending, setSending] = useState(false);
  const [actionError, setActionError] = useState("");
  const [actionNotice, setActionNotice] = useState("");
  const lastEvent = useRef(initialEvents.at(-1)?.id ?? 0);
  const [connection, setConnection] = useState<"connecting" | "live" | "reconnecting">("connecting");
  const [streamError, setStreamError] = useState(false);
  const correlationHeaders = useMemo(
    () => ({ "X-Kelpie-Correlation-ID": work.correlation_id }),
    [work.correlation_id],
  );

  useEffect(() => {
    let active = true;
    let stream: EventSource;
    let reconnect: ReturnType<typeof setTimeout> | undefined;

    async function refresh() {
      try {
        const [updated, evidence] = await Promise.all([
          apiJSON<WorkItem>(`${browserApi}/api/work-items/${work.id}`, { headers: correlationHeaders }),
          apiJSON<Artifact[]>(`${browserApi}/api/work-items/${work.id}/artifacts`, { headers: correlationHeaders }),
        ]);
        if (!active) return;
        setWork((current) => updated.version >= current.version ? updated : current);
        setArtifacts(evidence);
        setStreamError(false);
      } catch {
        if (active) setStreamError(true);
      }
    }

    function connect() {
      stream = new EventSource(`${browserApi}/api/work-items/${work.id}/events?after=${lastEvent.current}`, { withCredentials: true });
      stream.onopen = () => { setConnection("live"); void refresh(); };
      stream.onerror = () => {
        stream.close();
        if (!active) return;
        setConnection("reconnecting");
        reconnect = setTimeout(connect, 2000);
      };
      stream.onmessage = (message) => {
        if (!active) return;
        try {
          const event = JSON.parse(message.data) as AgentEvent;
          if (event.work_item_id !== work.id || !Number.isInteger(event.id)) return;
          lastEvent.current = Math.max(lastEvent.current, event.id);
          setEvents((current) => current.some((item) => item.id === event.id) ? current : [...current, event]);
          if (["work.transitioned", "artifact.uploaded"].includes(event.event_type)) void refresh();
        } catch {
          setStreamError(true);
        }
      };
    }
    connect();
    return () => { active = false; clearTimeout(reconnect); stream.close(); };
  }, [work.id, correlationHeaders]);

  const grouped = useMemo(() => [...events].reverse(), [events]);

  async function feedback(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (sending) return;
    setSending(true);
    setActionError("");
    setActionNotice("");
    const form = event.currentTarget;
    const data = new FormData(form);
    try {
      const updated = await apiJSON<WorkItem>(`${browserApi}/api/work-items/${work.id}/feedback`, {
        method: "POST", headers: { "content-type": "application/json", ...correlationHeaders },
        body: JSON.stringify({ message: data.get("message"), channel: "web" }),
      });
      setWork((current) => updated.version >= current.version ? updated : current);
      form.reset();
      setActionNotice(messages.run.feedbackSent);
    } catch (error) {
      setActionError(requestErrorMessage(error, messages.run.feedbackError, messages.run.networkError, messages.run.permissionError));
    } finally {
      setSending(false);
    }
  }

  async function approve() {
    if (sending) return;
    setSending(true);
    setActionError("");
    setActionNotice("");
    try {
      const updated = await apiJSON<WorkItem>(`${browserApi}/api/work-items/${work.id}/approvals`, {
        method: "POST", headers: { "content-type": "application/json", ...correlationHeaders },
        body: JSON.stringify({ kind: "pull_request", decision: "approve", payload: {} }),
      });
      setWork((current) => updated.version >= current.version ? updated : current);
      setActionNotice(messages.run.approvalRecorded);
    } catch (error) {
      setActionError(requestErrorMessage(error, messages.run.approvalError, messages.run.networkError, messages.run.permissionError));
    } finally {
      setSending(false);
    }
  }

  return (
    <>
      <section className="runHeader">
        <div>
          <p className="eyebrow">{work.repository} · {messages.source[work.source]}</p>
          <h1>{work.title}</h1>
          <p className="requirement">{work.requirement}</p>
        </div>
        <div className="runStatus">
          <span className={`status status-${work.status}`}>{statusLabel(work.status, messages.status)}</span>
          <strong>{statusProgress(work.status)}%</strong>
          <div className="progress"><i style={{ width: `${statusProgress(work.status)}%` }} /></div>
        </div>
      </section>
      <section className="runGrid">
        <div className="timeline">
          <div className="sectionHeading">
            <div><p className="eyebrow">{messages.run.liveStream}</p><h2>{messages.run.agentActivity}</h2></div>
            <span className={`liveDot connection-${connection}`} role="status">{messages.run[connection]}</span>
          </div>
          {streamError && <p className="streamError" role="alert">{messages.run.refreshError}</p>}
          <div className="events">
            {grouped.map((event) => (
              <article className={`event event-${event.level}`} key={event.id}>
                <div>
                  <span>{event.source}</span>
                  <LocalTime value={event.created_at} locale={locale} timeOnly />
                </div>
                <h3>{event.message || event.event_type}</h3>
                <p>{event.event_type}</p>
              </article>
            ))}
          </div>
        </div>
        <aside className="controlPanel">
          <p className="eyebrow">{messages.run.humanControl}</p><h2>{messages.run.title}</h2>
          {work.pull_request_url && (
            <a className="prLink" href={work.pull_request_url} target="_blank" rel="noreferrer">
              {messages.run.openPullRequest} <span>↗</span>
            </a>
          )}
          {work.status === "awaiting_approval" && (
            <button className="approve" disabled={sending} onClick={approve}>
              {messages.run.approve} <span>✓</span>
            </button>
          )}
          {actionError && <p className="formError" role="alert">{actionError}</p>}
          {actionNotice && <p className="actionNotice" role="status">{actionNotice}</p>}
          {artifacts.length > 0 && (
            <div className="artifactList">
              <p className="eyebrow">{messages.run.evidence}</p>
              {artifacts.map((artifact) => (
                <a
                  key={artifact.id}
                  href={`${browserApi}/api/work-items/${work.id}/artifacts/${artifact.id}`}
                  target="_blank"
                  rel="noreferrer"
                >
                  <span>{artifact.name}</span><small>{Math.ceil(artifact.size_bytes / 1024)} KB ↗</small>
                </a>
              ))}
            </div>
          )}
          <form onSubmit={feedback}>
            <label>
              {messages.run.feedback}
              <textarea name="message" rows={6} required placeholder={messages.run.feedbackPlaceholder} />
            </label>
            <button disabled={sending}>{messages.run.feedbackSubmit} <span>→</span></button>
          </form>
          <dl>
            <div><dt>{messages.run.worker}</dt><dd>{work.assigned_worker_id?.slice(0, 8) ?? messages.run.unassigned}</dd></div>
            <div><dt>{messages.run.budget}</dt><dd>{work.budget_minutes} {messages.run.minuteUnit}</dd></div>
            <div><dt>{messages.run.version}</dt><dd>{work.version}</dd></div>
          </dl>
        </aside>
      </section>
    </>
  );
}
