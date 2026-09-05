"use client";

import { useSyncExternalStore } from "react";
import { localeTag, type Locale } from "../i18n";
import { timestampWithZone } from "../lib/timestamp";

const subscribe = () => () => {};
const clientSnapshot = () => true;
const serverSnapshot = () => false;

export function LocalTime({ value, locale, timeOnly = false }: { value: string; locale: Locale; timeOnly?: boolean }) {
  // Initial text must not depend on either runtime's timezone or ICU locale data.
  const hydrated = useSyncExternalStore(subscribe, clientSnapshot, serverSnapshot);
  const timestamp = timestampWithZone(value);
  const date = new Date(timestamp);
  const iso = date.toISOString();
  let label = timeOnly ? `${iso.slice(11, 19)} UTC` : `${iso.slice(0, 16).replace("T", " ")} UTC`;
  if (hydrated) {
    label = timeOnly
      ? date.toLocaleTimeString(localeTag(locale))
      : date.toLocaleString(localeTag(locale), { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  }
  return <time dateTime={timestamp} title={iso}>{label}</time>;
}
