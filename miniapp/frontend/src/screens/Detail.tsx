import {
  ArrowLeft,
  ArrowUpRight,
  CalendarDays,
  Clock3,
  ExternalLink as ExternalIcon,
  FileText,
  Info,
} from "lucide-react";
import { FavoriteButton } from "../components/FavoriteButton";
import { api } from "../api";
import { useResource } from "../hooks";
import { categoryNames } from "../types";
import { formatDate, safeUrl, sourceLabel } from "../format";
import { ErrorState, ExternalLink, Skeletons } from "../components/common";

export function Detail({ id, onBack }: { id: string; onBack: () => void }) {
  const resource = useResource(
    (signal) => api.event(id, signal),
    `event-${id}`,
  );
  const event = resource.data;
  return (
    <div className="screen detail-screen">
      <button className="back-link" onClick={onBack}>
        <ArrowLeft size={18} />
        Назад
      </button>
      {resource.loading ? (
        <Skeletons count={1} />
      ) : resource.error ? (
        <ErrorState error={resource.error} retry={resource.retry} />
      ) : (
        event && (
          <>
            <div className="detail-heading">
              <div className="badge-row">
                {event.categories.map((category) => (
                  <span key={category} className={`category-badge ${category}`}>
                    {categoryNames[category] || category}
                  </span>
                ))}
              </div>
              <h1>{event.title || "Без названия"}</h1>
              <ExternalLink href={event.url} className="detail-source">
                <span className="source-mark">
                  <ExternalIcon size={13} />
                </span>
                {sourceLabel(event.url)}
                <ArrowUpRight size={15} />
              </ExternalLink>
              {!safeUrl(event.url) && (
                <p className="muted">Источник не указан</p>
              )}
            </div>
            <FavoriteButton id={event.id} saved={event.is_favorite} />
            <div className="date-facts">
              <div>
                <FileText size={18} />
                <span>
                  Опубликовано
                  <strong>
                    {formatDate(event.published_at) || "Дата неизвестна"}
                  </strong>
                </span>
              </div>
              <div>
                <CalendarDays size={18} />
                <span>
                  Мероприятие
                  <strong>
                    {formatDate(event.event_at, event.event_precision) ||
                      "Дата неизвестна"}
                  </strong>
                </span>
              </div>
              {event.deadline_at && (
                <div>
                  <Clock3 size={18} />
                  <span>
                    Приём заявок до
                    <strong>
                      {formatDate(
                        event.deadline_at,
                        event.deadline_precision,
                      ) || "Дата неизвестна"}
                    </strong>
                  </span>
                </div>
              )}
            </div>
            <section className="description-panel">
              <p className="eyebrow">О ВОЗМОЖНОСТИ</p>
              <h2>Что нужно знать</h2>
              <p className="event-description">
                {event.summary ||
                  "Описание отсутствует. Подробности можно уточнить в оригинальной публикации."}
              </p>
            </section>
            <div className="registration-note">
              <Info size={18} />
              <p>
                {event.registration_status === "closed"
                  ? "По сохранённым данным регистрация закрыта."
                  : event.registration_status === "open"
                    ? "По сохранённым данным регистрация открыта. Перед подачей заявки проверьте информацию в источнике."
                    : "Статус регистрации не подтверждён. Уточните условия в оригинальной публикации."}{" "}
                Даты указаны по московскому времени.
              </p>
            </div>
            <div className="detail-actions">
              <ExternalLink href={event.apply_url} className="button primary">
                Открыть регистрацию
                <ArrowUpRight size={19} />
              </ExternalLink>
              <ExternalLink href={event.url} className="button secondary">
                Открыть оригинал
                <ExternalIcon size={17} />
              </ExternalLink>
            </div>
          </>
        )
      )}
    </div>
  );
}
