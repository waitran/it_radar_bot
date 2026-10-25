import {
  ArrowRight,
  ArrowUpRight,
  Clock3,
  Code2,
  Radar,
  Sparkles,
} from "lucide-react";
import { useState } from "react";
import { api } from "../api";
import { useResource } from "../hooks";
import { formatDate, upcomingDeadlines } from "../format";
import { defaultFilters, type Category, type User } from "../types";
import {
  CategoryChips,
  EmptyState,
  ErrorState,
  SearchField,
  SectionHeading,
  Skeletons,
} from "../components/common";
import { EventCard } from "../components/EventCard";

export function Home({
  user,
  onCatalog,
  onEvent,
}: {
  user: User;
  onCatalog: (q?: string, category?: Category | "") => void;
  onEvent: (id: string) => void;
}) {
  const [search, setSearch] = useState("");
  const resource = useResource(
    (signal) => api.events(defaultFilters, 1, signal, 12),
    "home-events",
  );
  const deadlines = upcomingDeadlines(resource.data?.items || []);
  return (
    <div className="screen home-screen">
      <div className="greeting">
        <p>
          Привет, <span>{user.first_name}</span>
          <span className="greeting-dot" />{" "}
        </p>
        <span className="today">
          {new Intl.DateTimeFormat("ru-RU", {
            day: "numeric",
            month: "long",
            timeZone: "Europe/Moscow",
          }).format(new Date())}
        </span>
      </div>
      <section className="hero" aria-labelledby="hero-title">
        <div className="hero-content">
          <span className="hero-label">
            <span className="live-dot" />
            ТВОЙ НАВИГАТОР В IT
          </span>
          <h1 id="hero-title">
            Большое начинается
            <br />с <em>первого шага.</em>
          </h1>
          <p>
            Стажировки, события и конкурсы.
            <br />
            Всё, чтобы найти своё в мире технологий.
          </p>
          <button className="hero-cta" onClick={() => onCatalog()}>
            Найти возможность
            <ArrowUpRight size={18} />
          </button>
        </div>
        <div className="radar-art" aria-hidden="true">
          <div className="orbit orbit-one" />
          <div className="orbit orbit-two" />
          <div className="orbit orbit-three" />
          <div className="radar-sweep" />
          <span className="radar-center">
            <Radar size={28} strokeWidth={1.5} />
          </span>
          <span className="art-node node-code">
            <Code2 size={21} />
          </span>
          <span className="art-node node-star">
            <Sparkles size={19} />
          </span>
          <i className="orbit-point point-one" />
          <i className="orbit-point point-two" />
          <span className="art-caption">ОТКРЫВАЙ НОВОЕ</span>
        </div>
      </section>
      <SearchField
        value={search}
        onChange={setSearch}
        onSubmit={() => onCatalog(search)}
      />
      <CategoryChips
        value=""
        all={false}
        onChange={(category) => onCatalog("", category)}
      />
      <section className="content-section">
        <SectionHeading
          eyebrow="НА РАДАРЕ"
          title="Новые возможности"
          action="Весь каталог"
          onAction={() => onCatalog()}
        />
        {resource.loading ? (
          <Skeletons />
        ) : resource.error ? (
          <ErrorState error={resource.error} retry={resource.retry} />
        ) : resource.data?.items.length ? (
          <div className="event-grid">
            {resource.data.items.slice(0, 4).map((event) => (
              <EventCard key={event.id} event={event} onOpen={onEvent} />
            ))}
          </div>
        ) : (
          <EmptyState />
        )}
      </section>
      {deadlines.length > 0 && (
        <section className="content-section deadline-section">
          <SectionHeading
            eyebrow="ЕСТЬ ВРЕМЯ УСПЕТЬ"
            title="Ближайшие дедлайны"
          />
          <p className="section-note">
            Среди последних {resource.data?.items.length} публикаций. Все даты —
            по Москве.
          </p>
          <div className="deadline-grid">
            {deadlines.map((event) => (
              <button
                className="deadline-card"
                key={event.id}
                onClick={() => onEvent(event.id)}
              >
                <span className="deadline-label">
                  <Clock3 size={14} />
                  До {formatDate(event.deadline_at, event.deadline_precision)}
                </span>
                <strong>{event.title}</strong>
                <ArrowUpRight size={18} />
              </button>
            ))}
          </div>
        </section>
      )}
      <button className="catalog-banner" onClick={() => onCatalog()}>
        <span className="banner-icon">
          <Radar size={25} />
        </span>
        <span>
          <strong>Твоя возможность где-то рядом</strong>
          <small>Посмотреть весь каталог</small>
        </span>
        <ArrowRight size={21} />
      </button>
      <p className="bottom-note">Меньше шума. Больше возможностей.</p>
    </div>
  );
}
