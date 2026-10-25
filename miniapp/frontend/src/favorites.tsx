import {
  createContext,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { api } from "./api";

type FavoritesContextValue = {
  values: Record<string, boolean>;
  pending: Set<string>;
  errors: Record<string, unknown>;
  revision: number;
  toggle: (id: string, saved: boolean) => void;
  enabled: boolean;
};
const Context = createContext<FavoritesContextValue>({
  values: {},
  pending: new Set(),
  errors: {},
  revision: 0,
  toggle: () => {},
  enabled: false,
});
export const useFavorites = () => useContext(Context);

export function FavoritesProvider({
  userId,
  children,
}: {
  userId?: number;
  children: ReactNode;
}) {
  // Remount state when the verified principal changes, never share it between users.
  return (
    <FavoritesSession key={userId ?? "anonymous"} userId={userId}>
      {children}
    </FavoritesSession>
  );
}
function FavoritesSession({
  userId,
  children,
}: {
  userId?: number;
  children: ReactNode;
}) {
  const [values, setValues] = useState<Record<string, boolean>>({});
  const [pending, setPending] = useState<Set<string>>(new Set());
  const [errors, setErrors] = useState<Record<string, unknown>>({});
  const [revision, setRevision] = useState(0);
  const controllers = useRef(new Map<string, AbortController>());
  useEffect(
    () => () => {
      controllers.current.forEach((controller) => controller.abort());
    },
    [],
  );
  const toggle = (id: string, saved: boolean) => {
    if (!userId || controllers.current.has(id)) return;
    const controller = new AbortController();
    controllers.current.set(id, controller);
    setPending(new Set(controllers.current.keys()));
    setErrors((previous) => ({ ...previous, [id]: undefined }));
    api
      .setFavorite(id, !saved, controller.signal)
      .then(
        (result) => {
          if (!controller.signal.aborted) {
            setValues((previous) => ({
              ...previous,
              [id]: result.is_favorite,
            }));
            setRevision((value) => value + 1);
          }
        },
        (error) => {
          if (!controller.signal.aborted)
            setErrors((previous) => ({ ...previous, [id]: error }));
        },
      )
      .finally(() => {
        controllers.current.delete(id);
        if (!controller.signal.aborted)
          setPending(new Set(controllers.current.keys()));
      });
  };
  return (
    <Context.Provider
      value={{
        values,
        pending,
        errors,
        revision,
        toggle,
        enabled: Boolean(userId),
      }}
    >
      {children}
    </Context.Provider>
  );
}
