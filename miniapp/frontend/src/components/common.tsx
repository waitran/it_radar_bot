import type { ReactNode } from "react";
import {
  ArrowUpRight,
  BriefcaseBusiness,
  CalendarDays,
  Compass,
  GraduationCap,
  RefreshCw,
  Search,
  Trophy,
  WifiOff,
  X,
} from "lucide-react";
import { errorMessage } from "../api";
import { openInTelegram } from "../telegram";
import { safeUrl } from "../format";
import { categoryNames, type Category } from "../types";

export const categoryIcons = {
  internships: GraduationCap,
  events: CalendarDays,
  contests: Trophy,
  career: BriefcaseBusiness,
};
export function CategoryChips({
  value,
  onChange,
  all = true,
}: {
  value: Category | "";
  onChange: (value: Category | "") => void;
  all?: boolean;
}) {
  return (
    <div className="category-scroll" aria-label="Категории">
      {all && (
        <button
          className={`chip ${!value ? "selected" : ""}`}
          aria-pressed={!value}
          onClick={() => onChange("")}
        >
          <Compass size={16} />
          Все
        </button>
      )}
      {(Object.keys(categoryNames) as Category[]).map((category) => {
        const Icon = categoryIcons[category];
        return (
          <button
            key={category}
            className={`chip ${value === category ? "selected" : ""}`}
            aria-pressed={value === category}
            onClick={() => onChange(category)}
          >
            <Icon size={16} />
            {categoryNames[category]}
          </button>
        );
      })}
    </div>
  );
}
export function SearchField({
  value,
  onChange,
  onSubmit,
  placeholder = "Найти свой следующий шаг",
}: {
  value: string;
  onChange: (value: string) => void;
  onSubmit?: () => void;
  placeholder?: string;
}) {
  return (
    <form
      className="search-field"
      role="search"
      onSubmit={(event) => {
        event.preventDefault();
        onSubmit?.();
      }}
    >
      <Search size={20} aria-hidden="true" />
      <input
        aria-label="Поиск возможностей"
        type="search"
        value={value}
        maxLength={200}
        placeholder={placeholder}
        onChange={(event) => onChange(event.target.value)}
      />
      {value && (
        <button
          type="button"
          className="icon-button"
          aria-label="Очистить поиск"
          onClick={() => onChange("")}
        >
          <X size={17} />
        </button>
      )}
      {onSubmit && (
        <button type="submit" className="search-submit" aria-label="Найти">
          <ArrowUpRight size={20} />
        </button>
      )}
    </form>
  );
}
export function ExternalLink({
  href,
  children,
  className = "",
}: {
  href: string | null;
  children: ReactNode;
  className?: string;
}) {
  const url = safeUrl(href);
  if (!url) return null;
  return (
    <a
      href={url}
      target="_blank"
      rel="noopener noreferrer"
      className={className}
      onClick={(event) => {
        if (openInTelegram(url)) event.preventDefault();
      }}
    >
      {children}
    </a>
  );
}
export function ErrorState({
  error,
  retry,
}: {
  error: unknown;
  retry: () => void;
}) {
  return (
    <div className="state-panel" role="alert">
      <span className="state-icon">
        <WifiOff size={25} />
      </span>
      <h2>Не получилось загрузить</h2>
      <p>{errorMessage(error)}</p>
      <button className="button secondary" onClick={retry}>
        <RefreshCw size={16} />
        Повторить загрузку
      </button>
    </div>
  );
}
export function EmptyState({
  filtered = false,
  reset,
}: {
  filtered?: boolean;
  reset?: () => void;
}) {
  return (
    <div className="state-panel">
      <span className="state-icon">
        <Search size={26} />
      </span>
      <h2>
        {filtered ? "Пока ничего не нашлось" : "Новые возможности в пути"}
      </h2>
      <p>
        {filtered
          ? "Попробуйте другой запрос или измените фильтры. Ваш следующий шаг ещё впереди."
          : "Здесь появятся публикации из источников IT Radar. Загляните чуть позже."}
      </p>
      {reset && (
        <button className="button secondary" onClick={reset}>
          Сбросить фильтры
        </button>
      )}
    </div>
  );
}
export function Skeletons({ count = 4 }: { count?: number }) {
  return (
    <div
      className="event-grid"
      role="status"
      aria-label="Загрузка возможностей"
    >
      {Array.from({ length: count }, (_, i) => (
        <div className="event-card skeleton-card" key={i} aria-hidden="true">
          <div className="skeleton sk-tag" />
          <div className="skeleton sk-title" />
          <div className="skeleton sk-title short" />
          <div className="skeleton sk-line" />
          <div className="skeleton sk-line short" />
          <div className="skeleton sk-foot" />
        </div>
      ))}
    </div>
  );
}
export function SectionHeading({
  eyebrow,
  title,
  action,
  onAction,
}: {
  eyebrow?: string;
  title: string;
  action?: string;
  onAction?: () => void;
}) {
  return (
    <div className="section-heading">
      <div>
        {eyebrow && <p className="eyebrow">{eyebrow}</p>}
        <h2>{title}</h2>
      </div>
      {action && (
        <button className="text-button" onClick={onAction}>
          {action}
          <ArrowUpRight size={17} />
        </button>
      )}
    </div>
  );
}
