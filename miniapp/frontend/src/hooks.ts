import { useEffect, useState } from "react";

export function useDebounce<T>(value: T, delay = 400): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delay);
    return () => clearTimeout(timer);
  }, [value, delay]);
  return debounced;
}

export function useResource<T>(
  loader: (signal: AbortSignal) => Promise<T>,
  key: string,
): { data?: T; error?: unknown; loading: boolean; retry: () => void } {
  const [state, setState] = useState<{
    key: string;
    data?: T;
    error?: unknown;
    loading: boolean;
  }>({ key, loading: true });
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setState({ key, loading: true });
    loader(controller.signal).then(
      (data) => {
        if (!controller.signal.aborted) setState({ key, data, loading: false });
      },
      (error) => {
        if (!controller.signal.aborted)
          setState({ key, error, loading: false });
      },
    );
    return () => controller.abort();
    // The caller's key describes every input to loader. Avoid restarting on render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, attempt]);
  return {
    ...(state.key === key ? state : { loading: true }),
    retry: () => setAttempt((n) => n + 1),
  };
}
