// API model timestamps are UTC. SQLite can omit the offset on reload.
export function timestampWithZone(value: string): string {
  return /(?:Z|[+-]\d{2}:\d{2})$/i.test(value) ? value : `${value}Z`;
}
