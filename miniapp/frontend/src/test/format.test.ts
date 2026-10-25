import { describe, expect, it } from "vitest";
import { formatDate, safeUrl, sourceLabel, upcomingDeadlines } from "../format";
import { event } from "./fixtures";

describe("Честное отображение данных", () => {
  it("rejects unsafe URL protocols and embedded credentials", () => {
    for (const url of [
      "javascript:alert(1)",
      "file:///data/bot.db",
      "data:text/html,abc",
      "//example.org",
      "https://user:password@example.org",
    ])
      expect(safeUrl(url)).toBeNull();
    expect(safeUrl("https://example.org/register")).toBe(
      "https://example.org/register",
    );
  });
  it("derives only source identity present in the original URL", () => {
    expect(sourceLabel("https://t.me/s/my_channel/12")).toBe("@my_channel");
    expect(sourceLabel("https://www.example.org/events/1")).toBe("example.org");
    expect(sourceLabel(null)).toBe("Источник не указан");
  });
  it("keeps date-only precision and explicit Moscow timezone", () => {
    expect(formatDate(null)).toBeNull();
    expect(formatDate("invalid")).toBeNull();
    expect(formatDate("2026-10-25T20:59:59Z", "date")).toBe(
      "25 октября 2026 г.",
    );
    expect(formatDate("2026-10-25T12:00:00Z", "time")).toContain("15:00");
  });
  it("sorts future deadlines and excludes closed, past and dateless items", () => {
    const future = new Date(Date.now() + 86400000).toISOString();
    const later = new Date(Date.now() + 172800000).toISOString();
    expect(
      upcomingDeadlines([
        { ...event, id: "later", deadline_at: later },
        event,
        {
          ...event,
          id: "closed",
          deadline_at: future,
          registration_status: "closed",
        },
        { ...event, id: "past", deadline_at: "2020-01-01T00:00:00Z" },
        { ...event, id: "first", deadline_at: future },
      ]).map((item) => item.id),
    ).toEqual(["first", "later"]);
  });
});
