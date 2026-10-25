import type {
  Event,
  EventPage,
  Filters,
  User,
  FavoritePage,
  FavoriteState,
} from "./types";
import { getInitData } from "./telegram";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}
export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 401)
      return "Сессия Telegram истекла. Закройте Mini App и откройте его снова из бота.";
    if (error.status === 404)
      return "Возможно, эта публикация больше недоступна. Вернитесь в каталог.";
    if (error.status === 429)
      return "Слишком много запросов. Подождите немного и попробуйте снова.";
  }
  return "Не удалось связаться с IT Radar. Проверьте соединение и попробуйте ещё раз.";
}

// One app-wide lane: <= 4 request starts/second. Aborted queue entries never hit the network.
let lane: Promise<void> = Promise.resolve();
let lastStart = 0;
function wait(ms: number, signal: AbortSignal) {
  return new Promise<void>((resolve, reject) => {
    if (signal.aborted) {
      reject(new DOMException("Aborted", "AbortError"));
      return;
    }
    const onAbort = () => {
      clearTimeout(timer);
      reject(new DOMException("Aborted", "AbortError"));
    };
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    signal.addEventListener("abort", onAbort, { once: true });
  });
}
async function schedule(signal: AbortSignal) {
  const next = lane.then(async () => {
    signal.throwIfAborted();
    await wait(Math.max(0, 250 - (Date.now() - lastStart)), signal);
    signal.throwIfAborted();
    lastStart = Date.now();
  });
  lane = next.catch(() => {});
  return next;
}
export async function request<T>(
  path: string,
  signal: AbortSignal,
  method: "GET" | "PUT" | "DELETE" = "GET",
): Promise<T> {
  const initData = getInitData();
  if (!initData) throw new ApiError(401, "Telegram session required");
  const controller = new AbortController();
  const abort = () => controller.abort();
  if (signal.aborted) controller.abort();
  signal.addEventListener("abort", abort, { once: true });
  // Includes time in the queue, so stale requests do not linger indefinitely.
  const timer = setTimeout(abort, 12000);
  try {
    await schedule(controller.signal);
    const response = await fetch(`/api${path}`, {
      signal: controller.signal,
      method,
      headers: { Authorization: `tma ${initData}`, Accept: "application/json" },
      cache: "no-store",
      credentials: "omit",
      redirect: "error",
    });
    if (!response.ok) throw new ApiError(response.status, "API request failed");
    return (await response.json()) as T;
  } finally {
    clearTimeout(timer);
    signal.removeEventListener("abort", abort);
  }
}
export const api = {
  favorites: (page: number, signal: AbortSignal) =>
    request<FavoritePage>(`/favorites?page=${page}&page_size=12`, signal),
  setFavorite: (id: string, saved: boolean, signal: AbortSignal) =>
    request<FavoriteState>(
      `/favorites/${encodeURIComponent(id)}`,
      signal,
      saved ? "PUT" : "DELETE",
    ),
  me: (signal: AbortSignal) => request<User>("/me", signal),
  events: (filters: Filters, page: number, signal: AbortSignal, size = 12) => {
    const query = new URLSearchParams({
      sort: filters.sort,
      active_only: String(filters.active_only),
      page: String(page),
      page_size: String(size),
    });
    if (filters.q.trim()) query.set("q", filters.q.trim().slice(0, 200));
    if (filters.category) query.set("category", filters.category);
    return request<EventPage>(`/events?${query}`, signal);
  },
  event: (id: string, signal: AbortSignal) =>
    request<Event>(`/events/${encodeURIComponent(id)}`, signal),
};
