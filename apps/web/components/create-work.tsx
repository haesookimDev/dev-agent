"use client";

import { FormEvent, useState } from "react";
import { useRouter } from "next/navigation";
import type { Locale } from "../i18n";
import type { MessageCatalog } from "../i18n/types";
import { authenticatedFetch, browserApi } from "../lib/browser-api";
import { Icon } from "./icon";

export function CreateWork({
  locale,
  messages,
}: {
  locale: Locale;
  messages: MessageCatalog["create"];
}) {
  const router = useRouter();
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
    setError("");
    const data = new FormData(event.currentTarget);
    const response = await authenticatedFetch(`${browserApi}/api/work-items`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        title: data.get("title"),
        repository: data.get("repository"),
        requirement: data.get("requirement"),
      }),
    });
    if (!response.ok) {
      setError(`${messages.error} (${response.status})`);
      setSubmitting(false);
      return;
    }
    const work = await response.json();
    router.push(`/${locale}/work-items/${work.id}`);
    router.refresh();
  }

  return (
    <aside className="createPanel" id="create-work" aria-labelledby="create-title">
      <p className="eyebrow">{messages.eyebrow}</p>
      <h2 id="create-title">{messages.title}</h2>
      <p className="muted">{messages.description}</p>
      <form onSubmit={submit}>
        <label>
          {messages.repository}
          <input
            name="repository"
            placeholder={messages.repositoryPlaceholder}
            required
            pattern="[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+"
            maxLength={300}
            autoCapitalize="none"
            spellCheck={false}
          />
        </label>
        <label>
          {messages.workTitle}
          <input name="title" placeholder={messages.titlePlaceholder} required minLength={3} maxLength={300} />
        </label>
        <label>
          {messages.requirement}
          <textarea
            name="requirement"
            placeholder={messages.requirementPlaceholder}
            required
            minLength={3}
            maxLength={100000}
            rows={5}
          />
        </label>
        {error && <p className="formError" role="alert">{error}</p>}
        <button disabled={submitting}>
          {submitting ? messages.queuing : messages.submit}<Icon name="arrow" />
        </button>
      </form>
    </aside>
  );
}
