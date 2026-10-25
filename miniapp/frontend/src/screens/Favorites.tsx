import { Bookmark, ChevronLeft, ChevronRight } from "lucide-react";
import { useEffect } from "react";
import { api } from "../api";
import { useResource } from "../hooks";
import { useFavorites } from "../favorites";
import { EventCard } from "../components/EventCard";
import { FavoriteButton } from "../components/FavoriteButton";
import { ErrorState, Skeletons } from "../components/common";

export function Favorites({
  page,
  onPage,
  onEvent,
  onCatalog,
}: {
  page: number;
  onPage: (page: number) => void;
  onEvent: (id: string) => void;
  onCatalog: () => void;
}) {
  const state = useFavorites();
  const resource = useResource(
    (signal) => api.favorites(page, signal),
    `favorites-${page}-${state.revision}`,
  );
  const data = resource.data;
  const pages = Math.max(1, Math.ceil((data?.total || 0) / 12));
  useEffect(() => {
    if (data && data.items.length === 0 && page > 1)
      onPage(Math.max(1, Math.min(page - 1, pages)));
  }, [data, page, pages, onPage]);
  return (
    <div className="screen favorites-screen">
      <div className="page-heading">
        <p className="eyebrow">НА ТВОЁМ РАДАРЕ</p>
        <h1>
          Избранное<span className="heading-dot">.</span>
        </h1>
        <p>Сохрани то, к чему хочется вернуться.</p>
      </div>
      {resource.loading ? (
        <Skeletons />
      ) : resource.error ? (
        <ErrorState error={resource.error} retry={resource.retry} />
      ) : data?.items.length ? (
        <>
          <p className="favorites-count" aria-live="polite">
            Сохранено: {data.total}
          </p>
          <div className="event-grid">
            {data.items.map((favorite) =>
              favorite.event ? (
                <EventCard
                  key={favorite.item_key}
                  event={favorite.event}
                  onOpen={onEvent}
                  showOriginal
                />
              ) : (
                <article
                  className="event-card unavailable-card"
                  key={favorite.item_key}
                >
                  <div className="card-top">
                    <span className="category-badge">Недоступно</span>
                    <FavoriteButton id={favorite.item_key} saved compact />
                  </div>
                  <h2>Публикация больше недоступна</h2>
                  <p className="card-summary">
                    Её больше нет в каталоге. Вы можете удалить запись из
                    избранного.
                  </p>
                </article>
              ),
            )}
          </div>
          <nav className="pagination" aria-label="Страницы избранного">
            <button
              className="button secondary"
              disabled={page <= 1}
              onClick={() => onPage(page - 1)}
              aria-label="Предыдущая страница избранного"
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
              aria-label="Следующая страница избранного"
            >
              <ChevronRight size={18} />
            </button>
          </nav>
        </>
      ) : (
        <div className="state-panel">
          <span className="state-icon">
            <Bookmark size={26} />
          </span>
          <h2>Здесь будут твои находки</h2>
          <p>
            Нажми на закладку у интересной возможности. Сохранённое будет ждать
            тебя здесь.
          </p>
          <button className="button primary" onClick={onCatalog}>
            Перейти в каталог
          </button>
        </div>
      )}
    </div>
  );
}
