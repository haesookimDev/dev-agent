"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState, useTransition } from "react";
import { localeTag, type Locale } from "../i18n";
import type { MessageCatalog } from "../i18n/types";
import { statusLabel } from "../lib/status";
import { filterWorkItems, matchesFilter, type WorkFilter } from "../lib/work-list";
import type { WorkItem } from "../lib/types";
import { CreateWork } from "./create-work";
import { Icon } from "./icon";

export function DashboardWorkspace({ items, locale, messages }: {
  items: WorkItem[]; locale: Locale; messages: MessageCatalog;
}) {
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<WorkFilter>("all");
  const [refreshing, startRefresh] = useTransition();
  const router = useRouter();
  const labels = messages.dashboard;
  const visible = filterWorkItems(items, query, filter);
  const groups = [
    { id: "all", label: labels.totalWork, icon: "grid", tone: "blue" },
    { id: "active", label: labels.activeRuns, icon: "activity", tone: "blue" },
    { id: "attention", label: labels.needAttention, icon: "alert", tone: "amber" },
    { id: "completed", label: labels.completed, icon: "check", tone: "green" },
  ] as const;

  return <main id="main-content" tabIndex={-1} className="dashboardPage">
    <section className="pageHeading">
      <div><p className="eyebrow">{labels.eyebrow}</p><h1>{labels.title}</h1><p className="lede">{labels.introduction}</p></div>
      <a className="primaryButton" href="#create-work"><Icon name="plus" />{messages.create.eyebrow}</a>
    </section>
    <section className="summaryGrid" aria-label={labels.overview}>
      {groups.map((group) => <button key={group.id} className={`summaryCard ${group.tone}`} aria-pressed={filter === group.id} onClick={() => setFilter(group.id)}>
        <span className="summaryLabel"><span>{group.label}</span><Icon name={group.icon} /></span>
        <strong>{items.filter((item) => matchesFilter(item, group.id)).length}</strong>
        <span className="summaryHint">{group.id === "attention" ? labels.attentionHint : labels.viewWork}<Icon name="arrow" /></span>
      </button>)}
    </section>
    <section className="dashboardGrid">
      <div className="workPanel">
        <div className="sectionHeading">
          <div><h2>{labels.developmentWork}</h2><p className="panelDescription">{labels.recentHint}</p></div>
          <button className="iconButton" aria-label={refreshing ? labels.refreshing : labels.refresh} disabled={refreshing} onClick={() => startRefresh(() => router.refresh())}><Icon name="refresh" className={refreshing ? "spinning" : ""} /></button>
        </div>
        <div className="listToolbar">
          <label className="searchField"><Icon name="search" /><span className="srOnly">{labels.search}</span><input type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder={labels.search} /></label>
          <label className="filterField"><span className="srOnly">{labels.filter}</span><select value={filter} onChange={(event) => setFilter(event.target.value as WorkFilter)}>{groups.map((group) => <option value={group.id} key={group.id}>{group.label}</option>)}</select></label>
        </div>
        <p className="resultCount" role="status">{labels.results}: {visible.length}</p>
        {visible.length > 0 ? <div className="workList">
          <div className="workColumns" aria-hidden="true"><span>{labels.workColumn}</span><span>{labels.statusColumn}</span><span>{labels.updatedColumn}</span><span /></div>
          {visible.map((item) => <Link className="workRow" href={`/${locale}/work-items/${item.id}`} key={item.id}>
            <div className="workIdentity"><span className="repositoryIcon"><Icon name="git" /></span><div><h3>{item.title}</h3><p>{item.repository}<span className="sourceTag">{messages.source[item.source]}</span></p></div></div>
            <span className={`status status-${item.status}`}>{statusLabel(item.status, messages.status)}</span>
            <time dateTime={item.updated_at}>{new Date(item.updated_at).toLocaleString(localeTag(locale), { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}</time>
            <Icon name="arrow" className="rowArrow" />
          </Link>)}
        </div> : <div className="empty">
          <span className="emptyIcon"><Icon name={items.length ? "search" : "grid"} /></span>
          <h3>{items.length ? labels.noResults : labels.noWorkTitle}</h3>
          <p>{items.length ? labels.noResultsHint : labels.emptyHint}</p>
          {items.length ? <button className="secondaryButton" onClick={() => { setQuery(""); setFilter("all"); }}>{labels.clearFilters}</button> : <a className="secondaryButton" href="#create-work">{messages.create.eyebrow}<Icon name="plus" /></a>}
        </div>}
        <div className="panelFooter"><Icon name="shield" /><span>{labels.approvalHint}</span></div>
      </div>
      <CreateWork locale={locale} messages={messages.create} />
    </section>
  </main>;
}
