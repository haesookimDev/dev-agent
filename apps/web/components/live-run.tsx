"use client";

import { FormEvent, useEffect, useMemo, useState } from "react";
import { browserApi } from "../lib/api";
import { statusLabel, statusProgress } from "../lib/status";
import type { AgentEvent, Artifact, WorkItem } from "../lib/types";

export function LiveRun({ initialWork, initialEvents, initialArtifacts }: { initialWork: WorkItem; initialEvents: AgentEvent[]; initialArtifacts: Artifact[] }) {
  const [work, setWork] = useState(initialWork);
  const [events, setEvents] = useState(initialEvents);
  const [artifacts, setArtifacts] = useState(initialArtifacts);
  const [sending, setSending] = useState(false);
  const [actionError, setActionError] = useState("");
  const lastEvent = events.at(-1)?.id ?? 0;

  useEffect(() => {
    const stream = new EventSource(`${browserApi}/api/work-items/${work.id}/events?after=${lastEvent}`);
    stream.onmessage = (message) => {
      const event = JSON.parse(message.data) as AgentEvent;
      setEvents((current) => current.some((item) => item.id === event.id) ? current : [...current, event]);
      if (event.event_type === "work.transitioned") {
        fetch(`${browserApi}/api/work-items/${work.id}`).then((response) => response.json()).then(setWork);
      }
      if (event.event_type === "artifact.uploaded") {
        fetch(`${browserApi}/api/work-items/${work.id}/artifacts`).then((response) => response.json()).then(setArtifacts);
      }
    };
    return () => stream.close();
  }, [work.id, lastEvent]);

  const grouped = useMemo(() => [...events].reverse(), [events]);

  async function feedback(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSending(true);
    setActionError("");
    const form = event.currentTarget;
    const data = new FormData(form);
    const response = await fetch(`${browserApi}/api/work-items/${work.id}/feedback`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ message: data.get("message"), channel: "web" }),
    });
    if (response.ok) { setWork(await response.json()); form.reset(); }
    else setActionError((await response.json()).detail ?? "Feedback could not be sent.");
    setSending(false);
  }

  async function approve() {
    setSending(true);
    setActionError("");
    const response = await fetch(`${browserApi}/api/work-items/${work.id}/approvals`, {
      method: "POST", headers: { "content-type": "application/json", "X-Kelpie-Role": "approver" },
      body: JSON.stringify({ kind: "pull_request", decision: "approve", payload: {} }),
    });
    if (response.ok) setWork(await response.json());
    else setActionError((await response.json()).detail ?? "Approval could not be recorded.");
    setSending(false);
  }

  return (
    <>
      <section className="runHeader">
        <div><p className="eyebrow">{work.repository} · {work.source}</p><h1>{work.title}</h1><p className="requirement">{work.requirement}</p></div>
        <div className="runStatus"><span className={`status status-${work.status}`}>{statusLabel(work.status)}</span><strong>{statusProgress(work.status)}%</strong><div className="progress"><i style={{ width: `${statusProgress(work.status)}%` }} /></div></div>
      </section>
      <section className="runGrid">
        <div className="timeline">
          <div className="sectionHeading"><div><p className="eyebrow">Live stream</p><h2>Agent activity</h2></div><span className="liveDot">Live</span></div>
          <div className="events">
            {grouped.map((event) => <article className={`event event-${event.level}`} key={event.id}><div><span>{event.source}</span><time>{new Date(event.created_at).toLocaleTimeString()}</time></div><h3>{event.message || event.event_type}</h3><p>{event.event_type}</p></article>)}
          </div>
        </div>
        <aside className="controlPanel">
          <p className="eyebrow">Human control</p><h2>Steer the work.</h2>
          {work.pull_request_url && <a className="prLink" href={work.pull_request_url} target="_blank" rel="noreferrer">Open pull request <span>↗</span></a>}
          {work.status === "awaiting_approval" && <button className="approve" disabled={sending} onClick={approve}>Approve commit &amp; PR <span>✓</span></button>}
          {actionError && <p className="formError">{actionError}</p>}
          {artifacts.length > 0 && <div className="artifactList"><p className="eyebrow">Evidence</p>{artifacts.map((artifact) => <a key={artifact.id} href={`${browserApi}/api/work-items/${work.id}/artifacts/${artifact.id}`} target="_blank" rel="noreferrer"><span>{artifact.name}</span><small>{Math.ceil(artifact.size_bytes / 1024)} KB ↗</small></a>)}</div>}
          <form onSubmit={feedback}><label>Feedback<textarea name="message" rows={6} required placeholder="Describe what should be changed or checked…" /></label><button disabled={sending}>Send to agent <span>→</span></button></form>
          <dl><div><dt>Worker</dt><dd>{work.assigned_worker_id?.slice(0, 8) ?? "Unassigned"}</dd></div><div><dt>Budget</dt><dd>{work.budget_minutes} min</dd></div><div><dt>Version</dt><dd>{work.version}</dd></div></dl>
        </aside>
      </section>
    </>
  );
}
