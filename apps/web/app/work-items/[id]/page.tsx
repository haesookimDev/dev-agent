import Link from "next/link";
import { notFound } from "next/navigation";
import { LiveRun } from "../../../components/live-run";
import { getArtifacts, getEvents, getWorkItem } from "../../../lib/api";

export default async function WorkDetail({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const [work, events, artifacts] = await Promise.all([getWorkItem(id), getEvents(id), getArtifacts(id)]).catch(() => [null, [], []] as const);
  if (!work) notFound();
  return (
    <main className="detailPage">
      <Link className="back" href="/">← All work</Link>
      <LiveRun initialWork={work} initialEvents={events} initialArtifacts={artifacts} />
    </main>
  );
}
