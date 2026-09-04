import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import type { ReactNode } from "react";
import { LocaleSwitcher } from "../../components/locale-switcher";
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
        <header className="topbar">
          <Link className="brand" href={`/${locale}`}>
            <span className="brandMark">K</span>
            <span>Kelpie</span>
          </Link>
          <div className="topbarActions">
            <div className="environment"><span /> {messages.navigation.environment}</div>
            <LocaleSwitcher
              locale={locale}
              label={messages.navigation.switchLanguage}
              ariaLabel={messages.navigation.switchLanguageAria}
            />
          </div>
        </header>
        {children}
      </body>
    </html>
  );
}
