"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { defaultLocale, getMessages, isLocale } from "../i18n";
import { Icon } from "./icon";

export function PageState({ kind, retry }: { kind: "missing" | "error" | "loading"; retry?: () => void }) {
  const { locale: parameter } = useParams<{ locale?: string }>();
  const locale = parameter && isLocale(parameter) ? parameter : defaultLocale;
  const messages = getMessages(locale).pageState;
  const loading = kind === "loading";

  return (
    <main id="main-content" tabIndex={-1}>
      <section className="pageState" aria-busy={loading}>
        <span className="emptyIcon"><Icon name={loading ? "refresh" : "alert"} /></span>
        <h1>{messages[kind]}</h1>
        <p role={loading ? "status" : undefined}>{messages[`${kind}Hint`]}</p>
        {!loading && <div className="pageStateActions">
          {retry && <button className="primaryButton" onClick={retry}>{messages.retry}</button>}
          <Link className="secondaryButton" href={`/${locale}`}>{messages.back}</Link>
        </div>}
      </section>
    </main>
  );
}
