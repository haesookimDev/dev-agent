import Link from "next/link";
import { CreateWork } from "../components/create-work";
import { listWorkItems } from "../lib/api";
import { isAttentionStatus, statusLabel, statusProgress } from "../lib/status";

export default async function Dashboard() {
  const workItems = await listWorkItems().catch(() => []);
  const active = workItems.filter((item) => !["completed", "failed", "cancelled"].includes(item.status)).length;
  const attention = workItems.filter((item) => isAttentionStatus(item.status)).length;

  return (
    <main>
      <section className="hero">
        <div>
          <p className="eyebrow">Autonomous development operations</p>
          <h1>Work that keeps moving,<br /><em>under your control.</em></h1>
          <p className="lede">Route requirements into isolated machines, watch every decision, and approve only verified results.</p>
        </div>
        <div className="stats">
          <div><strong>{active}</strong><span>Active runs</span></div>
          <div><strong>{attention}</strong><span>Need attention</span></div>
          <div><strong>{workItems.length}</strong><span>Total work</span></div>
        </div>
      </section>

      <section className="dashboardGrid">
        <div className="workPanel">
          <div className="sectionHeading">
            <div><p className="eyebrow">Queue</p><h2>Development work</h2></div>
            <span className="count">{workItems.length}</span>
          </div>
          <div className="workList">
            {workItems.length === 0 ? (
              <div className="empty"><span>01</span><h3>No work yet</h3><p>Submit a requirement or apply the <code>agent-ready</code> label to a GitHub issue.</p></div>
            ) : workItems.map((item) => (
              <Link className="workCard" href={`/work-items/${item.id}`} key={item.id}>
                <div className="workTopline">
                  <span className={`status status-${item.status}`}>{statusLabel(item.status)}</span>
                  <time>{new Date(item.updated_at).toLocaleString()}</time>
                </div>
                <h3>{item.title}</h3>
                <p>{item.repository} · {item.source}</p>
                <div className="progress"><i style={{ width: `${statusProgress(item.status)}%` }} /></div>
              </Link>
            ))}
          </div>
        </div>
        <CreateWork />
      </section>
    </main>
  );
}
