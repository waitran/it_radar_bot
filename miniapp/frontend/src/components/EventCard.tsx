import { ArrowUpRight, CalendarDays, Clock3 } from "lucide-react";
import { categoryNames, type Event } from "../types";
import { formatDate, sourceLabel } from "../format";
import { categoryIcons, ExternalLink } from "./common";
import { FavoriteButton } from "./FavoriteButton";

export function EventCard({
  event,
  onOpen,
  showOriginal = false,
}: {
  event: Event;
  showOriginal?: boolean;
  onOpen: (id: string) => void;
}) {
  const category = event.categories[0];
  const Icon = categoryIcons[category] || CalendarDays;
  const deadline = formatDate(event.deadline_at, event.deadline_precision);
  const eventDate = formatDate(event.event_at, event.event_precision);
  return (
    <article className="event-card">
      <div className="card-top">
        <span className={`category-badge ${category || "events"}`}>
          <Icon size={14} />
          {categoryNames[category] || "Возможность"}
        </span>
        <FavoriteButton id={event.id} saved={event.is_favorite} compact />
      </div>
      <button className="card-title" onClick={() => onOpen(event.id)}>
        <h3>{event.title || "Без названия"}</h3>
        <span className="sr-only"> — открыть подробности</span>
      </button>
      <p className="card-summary">
        {event.summary || "Описание доступно в оригинальной публикации."}
      </p>
      {deadline ? (
        <p className="card-date">
          <Clock3 size={14} />
          <span>Подать заявку до {deadline}</span>
        </p>
      ) : eventDate ? (
        <p className="card-date">
          <CalendarDays size={14} />
          <span>{eventDate}</span>
        </p>
      ) : null}
      {showOriginal && (
        <ExternalLink href={event.url} className="card-original">
          Открыть оригинал
          <ArrowUpRight size={14} />
        </ExternalLink>
      )}
      <div className="card-footer">
        <span className="source-name">
          <span className="source-mark" aria-hidden="true">
            {sourceLabel(event.url).replace(/^@/, "").slice(0, 1).toUpperCase()}
          </span>
          {sourceLabel(event.url)}
        </span>
        <span>
          {formatDate(event.published_at)?.replace(/ 202\d г\./, "") ||
            "Дата не указана"}
        </span>
      </div>
    </article>
  );
}
