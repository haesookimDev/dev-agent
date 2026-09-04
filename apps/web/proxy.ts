import { NextRequest, NextResponse } from "next/server";
import { defaultLocale, isLocale, locales, type Locale } from "./i18n";

const publicFile = /\.[^/]+$/;

function preferredLocale(request: NextRequest): Locale {
  const cookieLocale = request.cookies.get("kelpie_locale")?.value;
  if (cookieLocale && isLocale(cookieLocale)) return cookieLocale;

  const accepted = request.headers.get("accept-language")?.toLowerCase() ?? "";
  const preference = accepted
    .split(",")
    .map((entry) => entry.split(";", 1)[0].trim().split("-", 1)[0])
    .find((language) => isLocale(language));
  return preference && isLocale(preference) ? preference : defaultLocale;
}

export function proxy(request: NextRequest) {
  const { pathname } = request.nextUrl;
  if (pathname.startsWith("/_next") || pathname.startsWith("/api") || publicFile.test(pathname)) {
    return NextResponse.next();
  }

  const pathLocale = pathname.split("/", 3)[1];
  if (isLocale(pathLocale)) {
    const response = NextResponse.next();
    response.cookies.set("kelpie_locale", pathLocale, {
      httpOnly: true,
      sameSite: "lax",
      maxAge: 60 * 60 * 24 * 365,
      path: "/",
    });
    return response;
  }

  const locale = preferredLocale(request);
  const url = request.nextUrl.clone();
  url.pathname = `/${locale}${pathname === "/" ? "" : pathname}`;
  return NextResponse.redirect(url);
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};

export { locales };
