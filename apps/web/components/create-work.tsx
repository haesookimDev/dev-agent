"use client";

import { FormEvent, useState } from "react";
import { useRouter } from "next/navigation";
import { browserApi } from "../lib/api";

export function CreateWork() {
  const router = useRouter();
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
    setError("");
    const data = new FormData(event.currentTarget);
    const response = await fetch(`${browserApi}/api/work-items`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        title: data.get("title"),
        repository: data.get("repository"),
        requirement: data.get("requirement"),
      }),
    });
    if (!response.ok) {
      setError(await response.text());
      setSubmitting(false);
      return;
    }
    const work = await response.json();
    router.push(`/work-items/${work.id}`);
    router.refresh();
  }

  return (
    <aside className="createPanel">
      <p className="eyebrow">New assignment</p>
      <h2>Describe the outcome.</h2>
      <p className="muted">The agent will inspect the repository and decide how to deliver it.</p>
      <form onSubmit={submit}>
        <label>Repository<input name="repository" placeholder="owner/repository" required pattern="[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+" /></label>
        <label>Title<input name="title" placeholder="What should change?" required minLength={3} /></label>
        <label>Requirement<textarea name="requirement" placeholder="Describe expected behavior, constraints, and acceptance criteria…" required minLength={3} rows={7} /></label>
        {error && <p className="formError">{error}</p>}
        <button disabled={submitting}>{submitting ? "Queuing…" : "Start development"}<span>↗</span></button>
      </form>
    </aside>
  );
}
