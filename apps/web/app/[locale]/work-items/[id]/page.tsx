import Link from "next/link";
import { notFound } from "next/navigation";
import { LiveRun } from "../../../../components/live-run";
import { getMessages, isLocale } from "../../../../i18n";
import { getArtifacts, getEvents, getWorkItem } from "../../../../lib/api";

export default async function WorkDetail({
  params,
}: {
  params: Promise<{ locale: string; id: string }>;
}) {
  const { locale, id } = await params;
  if (!isLocale(locale)) notFound();
  const messages = getMessages(locale);
  const [work, events, artifacts] = await Promise.all([
    getWorkItem(id),
    getEvents(id),
    getArtifacts(id),
  ]).catch(() => [null, [], []] as const);
  if (!work) notFound();

  return (
    <main className="detailPage">
      <Link className="back" href={`/${locale}`}>← {messages.run.back}</Link>
      <LiveRun
        initialWork={work}
        initialEvents={events}
        initialArtifacts={artifacts}
        locale={locale}
        messages={messages}
      />
    </main>
  );
}
