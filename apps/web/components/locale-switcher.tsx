"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { Locale } from "../i18n";

export function LocaleSwitcher({
  locale,
  label,
  ariaLabel,
}: {
  locale: Locale;
  label: string;
  ariaLabel: string;
}) {
  const pathname = usePathname();
  const target: Locale = locale === "ko" ? "en" : "ko";
  const href = pathname.replace(/^\/(ko|en)(?=\/|$)/, `/${target}`);

  return (
    <Link className="localeSwitch" href={href || `/${target}`} hrefLang={target} aria-label={ariaLabel}>
      {label}
    </Link>
  );
}
