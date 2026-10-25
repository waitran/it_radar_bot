export type Category = "internships" | "events" | "contests" | "career";
export type Sort = "asc" | "desc";
export interface User {
  id: number;
  first_name: string;
  last_name: string | null;
  username: string | null;
  language_code: string | null;
}
export interface Event {
  id: string;
  title: string;
  summary: string;
  url: string | null;
  apply_url: string | null;
  categories: Category[];
  published_at: string | null;
  event_at: string | null;
  deadline_at: string | null;
  event_precision: "date" | "time" | null;
  deadline_precision: "date" | "time" | null;
  registration_status: string;
  is_favorite: boolean;
}
export interface EventPage {
  items: Event[];
  total: number;
  page: number;
  page_size: number;
}
export interface Filters {
  q: string;
  category: Category | "";
  active_only: boolean;
  sort: Sort;
}
export const defaultFilters: Filters = {
  q: "",
  category: "",
  active_only: false,
  sort: "desc",
};
export const categoryNames: Record<Category, string> = {
  internships: "Стажировки",
  events: "Мероприятия",
  contests: "Конкурсы",
  career: "Карьера",
};

export interface Favorite {
  item_key: string;
  created_at: string;
  event: Event | null;
}
export interface FavoritePage {
  items: Favorite[];
  total: number;
  page: number;
  page_size: number;
}
export interface FavoriteState {
  id: string;
  is_favorite: boolean;
}
