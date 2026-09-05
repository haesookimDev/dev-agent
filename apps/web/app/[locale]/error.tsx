"use client";

import { PageState } from "../../components/page-state";

export default function ErrorPage({ retry }: { error: Error & { digest?: string }; retry: () => void }) {
  return <PageState kind="error" retry={retry} />;
}
