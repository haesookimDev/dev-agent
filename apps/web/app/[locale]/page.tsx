import Link from "next/link";
import { notFound } from "next/navigation";
import { CreateWork } from "../../components/create-work";
import { getMessages, isLocale, localeTag } from "../../i18n";
import { listWorkItems } from "../../lib/api";
import { isAttentionStatus, statusLabel, statusProgress } from "../../lib/status";

export default async function Dashboard({ params }: { params: Promise<{ locale: string }> }) {
  const { locale } = await params;
  if (!isLocale(locale)) notFound();
  const messages = getMessages(locale);
  const workItems = await listWorkItems(`/${locale}`);
  const active = workItems.filter(
    (item) => !["completed", "failed", "cancelled"].includes(item.status),
  ).length;
  const attention = workItems.filter((item) => isAttentionStatus(item.status)).length;

  return (
    <main>
      <section className="hero">
        <div>
          <p className="eyebrow">{messages.dashboard.eyebrow}</p>
          <h1>{messages.dashboard.heroLine}<br /><em>{messages.dashboard.heroEmphasis}</em></h1>
          <p className="lede">{messages.dashboard.introduction}</p>
        </div>
        <div className="stats">
          <div><strong>{active}</strong><span>{messages.dashboard.activeRuns}</span></div>
          <div><strong>{attention}</strong><span>{messages.dashboard.needAttention}</span></div>
          <div><strong>{workItems.length}</strong><span>{messages.dashboard.totalWork}</span></div>
        </div>
      </section>

      <section className="dashboardGrid">
        <div className="workPanel">
          <div className="sectionHeading">
            <div><p className="eyebrow">{messages.dashboard.queue}</p><h2>{messages.dashboard.developmentWork}</h2></div>
            <span className="count">{workItems.length}</span>
          </div>
          <div className="workList">
            {workItems.length === 0 ? (
              <div className="empty">
                <span>01</span>
                <h3>{messages.dashboard.noWorkTitle}</h3>
                <p>{messages.dashboard.noWorkBeforeLabel} <code>agent-ready</code> {messages.dashboard.noWorkAfterLabel}</p>
              </div>
            ) : workItems.map((item) => (
              <Link className="workCard" href={`/${locale}/work-items/${item.id}`} key={item.id}>
                <div className="workTopline">
                  <span className={`status status-${item.status}`}>{statusLabel(item.status, messages.status)}</span>
                  <time>{new Date(item.updated_at).toLocaleString(localeTag(locale))}</time>
                </div>
                <h3>{item.title}</h3>
                <p>{item.repository} · {messages.source[item.source]}</p>
                <div className="progress"><i style={{ width: `${statusProgress(item.status)}%` }} /></div>
              </Link>
            ))}
          </div>
        </div>
        <CreateWork locale={locale} messages={messages.create} />
      </section>
    </main>
  );
}
