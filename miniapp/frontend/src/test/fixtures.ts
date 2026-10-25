// Test-only publications. Never imported by the application entry point.
import type { Event, EventPage, User } from "../types";
export const user: User = {
  id: 42,
  first_name: "Анна",
  last_name: "Иванова",
  username: "anna_test",
  language_code: "ru",
};
export const event: Event = {
  id: "test:1",
  title: "Тестовый Python-митап",
  summary: "Описание тестового мероприятия",
  url: "https://t.me/test_channel/12",
  apply_url: "https://example.org/register",
  categories: ["events"],
  published_at: "2026-10-20T12:00:00Z",
  event_at: null,
  deadline_at: null,
  event_precision: null,
  deadline_precision: null,
  registration_status: "unknown",
  is_favorite: false,
};
export const result: EventPage = {
  items: [event],
  total: 1,
  page: 1,
  page_size: 12,
};
