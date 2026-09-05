export interface PreviewLaunch {
  launch_code: string;
  exchange_url: string;
  expires_at: string;
}

export function previewExchangeURL(launch: PreviewLaunch, workId: string): string {
  const url = new URL(launch.exchange_url);
  if (url.protocol !== "https:" || url.username || url.password || url.search || url.hash ||
      url.pathname !== "/_kelpie/authorize" || !url.hostname.startsWith(`${workId}.`) ||
      !/^kpl_[A-Za-z0-9_-]{43}$/.test(launch.launch_code) || !Number.isFinite(Date.parse(launch.expires_at))) {
    throw new Error("Invalid Preview launch response");
  }
  return url.href;
}
