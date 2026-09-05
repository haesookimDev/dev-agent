"use client";

import { useSyncExternalStore } from "react";
import { localeTag, type Locale } from "../i18n";
import { timestampWithZone } from "../lib/timestamp";

const subscribe = () => () => {};
const browserZone = () => Intl.DateTimeFormat().resolvedOptions().timeZone;
const serverZone = () => "UTC";

export function LocalTime({ value, locale, timeOnly = false }: { value: string; locale: Locale; timeOnly?: boolean }) {
  // Hydration starts with the same UTC snapshot as SSR, then uses browser time.
  const timeZone = useSyncExternalStore(subscribe, browserZone, serverZone);
  const timestamp = timestampWithZone(value);
  const date = new Date(timestamp);
  const label = timeOnly
    ? date.toLocaleTimeString(localeTag(locale), { timeZone })
    : date.toLocaleString(localeTag(locale), { timeZone, month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  return <time dateTime={timestamp} title={date.toISOString()}>{label}</time>;
}
