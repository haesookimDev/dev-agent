import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import type { ReactNode } from "react";
import { LocaleSwitcher } from "../../components/locale-switcher";
import { Icon } from "../../components/icon";
import { getMessages, isLocale, locales } from "../../i18n";
import "../globals.css";

type LayoutProps = Readonly<{
  children: ReactNode;
  params: Promise<{ locale: string }>;
}>;

export function generateStaticParams() {
  return locales.map((locale) => ({ locale }));
}

export async function generateMetadata({ params }: LayoutProps): Promise<Metadata> {
  const { locale } = await params;
  if (!isLocale(locale)) return {};
  const messages = getMessages(locale);
  return {
    title: messages.metadata.title,
    description: messages.metadata.description,
  };
}

export default async function LocaleLayout({ children, params }: LayoutProps) {
  const { locale } = await params;
  if (!isLocale(locale)) notFound();
  const messages = getMessages(locale);

  return (
    <html lang={locale}>
      <body>
        <a className="skipLink" href="#main-content">{messages.navigation.skipContent}</a>
        <aside className="sidebar">
          <Link className="brand" href={`/${locale}`}>
            <span className="brandMark">K</span>
            <span>Kelpie</span>
          </Link>
          <p className="navLabel">{messages.navigation.workspace}</p>
          <nav aria-label={messages.navigation.workspace}>
            <Link className="navLink" href={`/${locale}`}><Icon name="grid" />{messages.navigation.overview}</Link>
            <Link className="navLink" href={`/${locale}#create-work`}><Icon name="plus" />{messages.create.eyebrow}</Link>
          </nav>
          <div className="sidebarNote"><Icon name="shield" /><strong>{messages.navigation.controlled}</strong><p>{messages.navigation.controlledHint}</p></div>
        </aside>
        <div className="workspaceShell">
        <header className="topbar">
          <span className="workspaceTitle">{messages.navigation.workspace}<span>/</span>{messages.navigation.overview}</span>
          <div className="topbarActions">
            <LocaleSwitcher
              locale={locale}
              label={messages.navigation.switchLanguage}
              ariaLabel={messages.navigation.switchLanguageAria}
            />
          </div>
        </header>
        {children}
        <footer className="workspaceFooter">Kelpie<span>{messages.navigation.footer}</span></footer>
        </div>
      </body>
    </html>
  );
}
