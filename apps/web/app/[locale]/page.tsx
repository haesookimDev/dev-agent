import { notFound } from "next/navigation";
import { DashboardWorkspace } from "../../components/dashboard-workspace";
import { getMessages, isLocale } from "../../i18n";
import { listWorkItems } from "../../lib/api";

export default async function Dashboard({ params }: { params: Promise<{ locale: string }> }) {
  const { locale } = await params;
  if (!isLocale(locale)) notFound();
  const messages = getMessages(locale);
  const workItems = await listWorkItems(`/${locale}`);
  return <DashboardWorkspace items={workItems} locale={locale} messages={messages} />;
}
