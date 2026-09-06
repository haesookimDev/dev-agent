"use client";

import { useCallback, useEffect, useId, useRef, useState } from "react";
import type { Locale } from "../i18n";
import type { MessageCatalog } from "../i18n/types";
import { artifactURL, ArtifactPreviewError, loadArtifactPreview, type ArtifactPreview, type PreviewFailure } from "../lib/artifact-preview";
import type { Artifact } from "../lib/types";

type Messages = MessageCatalog["artifacts"];
type PreviewState = { phase: "loading" } | { phase: "error"; reason: PreviewFailure } |
  { phase: "ready"; content: ArtifactPreview; imageURL?: string };

export function artifactSize(size: number, locale: Locale): string {
  if (!Number.isFinite(size) || size < 0) return "—";
  const unit = size >= 1024 * 1024 ? "MiB" : size >= 1024 ? "KiB" : "B";
  const value = size / (unit === "MiB" ? 1024 * 1024 : unit === "KiB" ? 1024 : 1);
  return `${new Intl.NumberFormat(locale, { maximumFractionDigits: 1 }).format(value)} ${unit}`;
}

function PreviewDialog({ workId, artifact, locale, messages, onDismiss }: {
  workId: string; artifact: Artifact; locale: Locale; messages: Messages; onDismiss: () => void;
}) {
  const id = useId();
  const dialog = useRef<HTMLDialogElement>(null);
  const imageURL = useRef<string | null>(null);
  const request = useRef<AbortController | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [state, setState] = useState<PreviewState>({ phase: "loading" });
  const clearImage = useCallback(() => {
    if (imageURL.current) URL.revokeObjectURL(imageURL.current);
    imageURL.current = null;
  }, []);

  useEffect(() => {
    const element = dialog.current!;
    element.showModal();
    return () => element.close();
  }, []);

  useEffect(() => {
    let active = true;
    const controller = new AbortController();
    request.current = controller;
    void loadArtifactPreview(workId, artifact.id, controller.signal).then((content) => {
      if (!active) return;
      if (content.kind === "image") imageURL.current = URL.createObjectURL(content.blob);
      setState({ phase: "ready", content, imageURL: imageURL.current ?? undefined });
    }).catch((error: unknown) => {
      if (!active) return;
      clearImage();
      setState({ phase: "error", reason: error instanceof ArtifactPreviewError ? error.reason : "network" });
    });
    return () => { active = false; controller.abort(); clearImage(); };
  }, [workId, artifact.id, attempt, clearImage]);

  function close() {
    request.current?.abort();
    clearImage();
    dialog.current?.close();
    onDismiss();
  }

  function retry() {
    clearImage();
    setState({ phase: "loading" });
    setAttempt((value) => value + 1);
  }

  return (
    <dialog ref={dialog} className="artifactPreview" aria-labelledby={`${id}-name`} aria-describedby={`${id}-hint`}
      onCancel={(event) => { event.preventDefault(); close(); }}
      onKeyDown={(event) => {
        if (event.key !== "Tab") return;
        const controls = event.currentTarget.querySelectorAll<HTMLElement>("button:not([disabled]), a[href], [tabindex='0']");
        const first = controls[0];
        const last = controls[controls.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
      }}>
      <header className="previewHeading">
        <div><p className="eyebrow">{messages.preview}</p><h2 id={`${id}-name`}>{artifact.name}</h2></div>
        <button className="secondaryButton previewClose" type="button" onClick={close}>{messages.close} <span aria-hidden="true">×</span></button>
      </header>
      <div className="previewMetadata">
        <span>{state.phase === "ready" ? state.content.mediaType : artifact.content_type}</span>
        <span>{artifactSize(state.phase === "ready" ? state.content.size : artifact.size_bytes, locale)}</span>
      </div>
      <div className="previewBody" role="region" aria-label={messages.content} tabIndex={0} aria-busy={state.phase === "loading"}>
        {state.phase === "loading" && <p className="previewStatus" role="status">{messages.loading}</p>}
        {state.phase === "error" && <div className="previewFailure">
          <p className="eyebrow">{messages.errorTitle}</p>
          <p role="alert">{messages.errors[state.reason]}</p>
          <button className="secondaryButton" type="button" onClick={retry}>{messages.retry}</button>
        </div>}
        {state.phase === "ready" && (state.content.kind === "text" ? (
          state.content.text.length === 0 ? <p className="previewStatus" role="status">{messages.emptyFile}</p> :
            <pre className="previewText">{state.content.text}</pre>
        ) : (
          // Blob URLs need neither the Next image proxy nor executable document embedding.
          // eslint-disable-next-line @next/next/no-img-element
          <img className="previewImage" src={state.imageURL} alt={`${messages.image}: ${artifact.name}`}
            onError={() => { clearImage(); setState({ phase: "error", reason: "invalid" }); }} />
        ))}
      </div>
      <footer className="previewFooter">
        <p id={`${id}-hint`}>{messages.hint}</p>
        <a className="secondaryButton" href={artifactURL(workId, artifact.id)} target="_blank" rel="noreferrer">{messages.original} ↗</a>
      </footer>
    </dialog>
  );
}

export function ArtifactEvidence({ workId, artifacts, locale, messages }: {
  workId: string; artifacts: Artifact[]; locale: Locale; messages: Messages;
}) {
  const heading = useId();
  const opener = useRef<HTMLButtonElement | null>(null);
  const [selected, setSelected] = useState<Artifact | null>(null);
  return (
    <section className="artifactList" aria-labelledby={heading}>
      <div className="artifactListHeading"><h3 id={heading}>{messages.title}</h3><span>{artifacts.length}</span></div>
      {artifacts.length === 0 ? <p className="artifactEmpty">{messages.empty}</p> : (
        <ul>{artifacts.map((artifact) => <li className="artifactRow" key={artifact.id}>
          <button className="previewTrigger" type="button" aria-haspopup="dialog" aria-label={`${messages.open}: ${artifact.name}`}
            onClick={(event) => { opener.current = event.currentTarget; setSelected(artifact); }}>
            <span className="artifactName">{artifact.name}</span>
            <span className="artifactSummary">{artifact.content_type} · {artifactSize(artifact.size_bytes, locale)}</span>
            <span className="artifactAction">{messages.open} <span aria-hidden="true">→</span></span>
          </button>
          <a className="artifactOriginal" href={artifactURL(workId, artifact.id)} target="_blank" rel="noreferrer"
            aria-label={`${messages.original}: ${artifact.name}`}>{messages.original} ↗</a>
        </li>)}</ul>
      )}
      {selected && <PreviewDialog key={selected.id} workId={workId} artifact={selected} locale={locale} messages={messages}
        onDismiss={() => { setSelected(null); opener.current?.focus(); }} />}
    </section>
  );
}
