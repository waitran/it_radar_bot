import { Bookmark, Check, LoaderCircle } from "lucide-react";
import { useFavorites } from "../favorites";
import { errorMessage } from "../api";

export function FavoriteButton({
  id,
  saved,
  compact = false,
}: {
  id: string;
  saved: boolean;
  compact?: boolean;
}) {
  const state = useFavorites();
  const selected = state.values[id] ?? saved;
  const pending = state.pending.has(id);
  const error = state.errors[id];
  const label = selected ? "Удалить из избранного" : "Сохранить в избранное";
  return (
    <div className={`favorite-control ${compact ? "compact" : ""}`}>
      <button
        type="button"
        className={`favorite-button ${selected ? "saved" : ""} ${compact ? "" : "button secondary"}`}
        aria-label={label}
        aria-pressed={selected}
        aria-busy={pending}
        disabled={pending || !state.enabled}
        title={pending ? "Сохраняем…" : label}
        onClick={() => state.toggle(id, selected)}
      >
        {pending ? (
          <LoaderCircle size={18} className="favorite-spinner" />
        ) : (
          <Bookmark size={18} fill={selected ? "currentColor" : "none"} />
        )}
        {!compact && (
          <span>
            {pending ? "Сохраняем…" : selected ? "В избранном" : "В избранное"}
          </span>
        )}
        {!compact && selected && !pending && <Check size={14} />}
      </button>
      {Boolean(error) && (
        <span className="favorite-error" role="alert">
          {errorMessage(error)} Нажмите ещё раз для повтора.
        </span>
      )}
    </div>
  );
}
