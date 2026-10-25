import type { Event } from "./types";

export const DISPLAY_TIMEZONE = "Europe/Moscow";
export function formatDate(
  value: string | null,
  precision: "date" | "time" | null = "date",
): string | null {
  if (!value || !Number.isFinite(Date.parse(value))) return null;
  return (
    new Intl.DateTimeFormat("ru-RU", {
      day: "numeric",
      month: "long",
      year: "numeric",
      timeZone: DISPLAY_TIMEZONE,
      ...(precision === "time" ? { hour: "2-digit", minute: "2-digit" } : {}),
    }).format(new Date(value)) + (precision === "time" ? " мск" : "")
  );
}
export function safeUrl(value: string | null | undefined): string | null {
  try {
    if (!value) return null;
    const url = new URL(value);
    return ["https:", "http:"].includes(url.protocol) &&
      !url.username &&
      !url.password
      ? url.href
      : null;
  } catch {
    return null;
  }
}
export function sourceLabel(value: string | null): string {
  const safe = safeUrl(value);
  if (!safe) return "Источник не указан";
  const url = new URL(safe);
  if (url.hostname === "t.me") {
    const segments = url.pathname.split("/").filter(Boolean);
    const channel = segments[0] === "s" ? segments[1] : segments[0];
    return channel &&
      !["c", "joinchat"].includes(channel) &&
      !channel.startsWith("+")
      ? `@${channel}`
      : "Telegram";
  }
  return url.hostname.replace(/^www\./, "");
}
export function upcomingDeadlines(items: Event[], now = Date.now()): Event[] {
  return items
    .filter(
      (item) =>
        item.deadline_at &&
        Date.parse(item.deadline_at) >= now &&
        item.registration_status !== "closed" &&
        (!item.event_at || Date.parse(item.event_at) >= now),
    )
    .sort((a, b) => Date.parse(a.deadline_at!) - Date.parse(b.deadline_at!))
    .slice(0, 3);
}
