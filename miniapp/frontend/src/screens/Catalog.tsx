import {
  ArrowDownWideNarrow,
  ChevronLeft,
  ChevronRight,
  SlidersHorizontal,
} from "lucide-react";
import { api } from "../api";
import { useDebounce, useResource } from "../hooks";
import type { Filters } from "../types";
import { defaultFilters } from "../types";
import {
  CategoryChips,
  EmptyState,
  ErrorState,
  SearchField,
  Skeletons,
} from "../components/common";
import { EventCard } from "../components/EventCard";

export function Catalog({
  filters,
  page,
  onChange,
  onPage,
  onEvent,
}: {
  filters: Filters;
  page: number;
  onChange: (filters: Filters) => void;
  onPage: (page: number) => void;
  onEvent: (id: string) => void;
}) {
  const q = useDebounce(filters.q);
  const key = JSON.stringify({ ...filters, q, page });
  const resource = useResource(
    (signal) => api.events({ ...filters, q }, page, signal),
    key,
  );
  const filtered = Boolean(
    filters.q || filters.category || filters.active_only,
  );
  const total = resource.data?.total;
  const pages = Math.max(1, Math.ceil((total || 0) / 12));
  function reset() {
    onChange(defaultFilters);
  }
  return (
    <div className="screen catalog-screen">
      <div className="page-heading">
        <p className="eyebrow">НАЙДИ СВОЁ</p>
        <h1>
          Каталог возможностей<span className="heading-dot">.</span>
        </h1>
        <p>Следующий шаг в IT начинается здесь.</p>
      </div>
      <SearchField
        value={filters.q}
        onChange={(value) => onChange({ ...filters, q: value })}
        placeholder="Название или описание"
      />
      <CategoryChips
        value={filters.category}
        onChange={(category) => onChange({ ...filters, category })}
      />
      <div className="filter-bar">
        <label className="active-toggle">
          <input
            type="checkbox"
            checked={filters.active_only}
            onChange={(event) =>
              onChange({ ...filters, active_only: event.target.checked })
            }
          />
          <span className="switch" />
          <span>Только актуальные</span>
        </label>
        <label className="sort-select">
          <ArrowDownWideNarrow size={17} />
          <span className="sr-only">Сортировка публикаций</span>
          <select
            aria-label="Сортировка публикаций"
            value={filters.sort}
            onChange={(event) =>
              onChange({
                ...filters,
                sort: event.target.value as Filters["sort"],
              })
            }
          >
            <option value="desc">Сначала новые</option>
            <option value="asc">Сначала старые</option>
          </select>
        </label>
      </div>
      <div className="results-meta" aria-live="polite">
        <span>
          {resource.loading || q !== filters.q
            ? "Ищем возможности…"
            : total !== undefined
              ? `Найдено: ${total}`
              : "Возможности"}
        </span>
        {filtered && (
          <button className="text-button" onClick={reset}>
            <SlidersHorizontal size={14} />
            Сбросить
          </button>
        )}
      </div>
      {filters.active_only && (
        <p className="filter-hint">
          Показываем публикации с известной будущей датой события или дедлайном.
          Статус регистрации уточняйте в оригинале.
        </p>
      )}
      {resource.loading || q !== filters.q ? (
        <Skeletons />
      ) : resource.error ? (
        <ErrorState error={resource.error} retry={resource.retry} />
      ) : resource.data?.items.length ? (
        <>
          <div className="event-grid">
            {resource.data.items.map((event) => (
              <EventCard key={event.id} event={event} onOpen={onEvent} />
            ))}
          </div>
          <nav className="pagination" aria-label="Страницы каталога">
            <button
              className="button secondary"
              disabled={page <= 1}
              onClick={() => onPage(page - 1)}
              aria-label="Предыдущая страница"
            >
              <ChevronLeft size={18} />
            </button>
            <span>
              Страница <strong>{page}</strong> из {pages}
            </span>
            <button
              className="button secondary"
              disabled={page >= pages}
              onClick={() => onPage(page + 1)}
              aria-label="Следующая страница"
            >
              <ChevronRight size={18} />
            </button>
          </nav>
        </>
      ) : (
        <EmptyState
          filtered={filtered || page > 1}
          reset={filtered || page > 1 ? reset : undefined}
        />
      )}
    </div>
  );
}
