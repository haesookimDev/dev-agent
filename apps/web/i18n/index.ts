import { en } from "./messages/en";
import { ko } from "./messages/ko";
import type { MessageCatalog } from "./types";

export const locales = ["ko", "en"] as const;
export type Locale = (typeof locales)[number];
export const defaultLocale: Locale = "ko";

const messages: Record<Locale, MessageCatalog> = { en, ko };

export function isLocale(value: string): value is Locale {
  return locales.some((locale) => locale === value);
}

export function getMessages(locale: Locale): MessageCatalog {
  return messages[locale];
}

export function localeTag(locale: Locale): string {
  return locale === "ko" ? "ko-KR" : "en-US";
}
