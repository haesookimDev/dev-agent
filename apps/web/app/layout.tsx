import type { Metadata } from "next";
import Link from "next/link";
import type { ReactNode } from "react";
import "./globals.css";

export const metadata: Metadata = {
  title: "Kelpie · Autonomous development control plane",
  description: "Observe, steer, verify, and approve autonomous development work.",
};

export default function RootLayout({ children }: Readonly<{ children: ReactNode }>) {
  return (
    <html lang="en">
      <body>
        <header className="topbar">
          <Link className="brand" href="/">
            <span className="brandMark">K</span>
            <span>Kelpie</span>
          </Link>
          <div className="environment"><span /> Local control plane</div>
        </header>
        {children}
      </body>
    </html>
  );
}
