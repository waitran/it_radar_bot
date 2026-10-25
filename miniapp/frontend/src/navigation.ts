import { useCallback, useEffect, useState } from "react";
export type Route =
  | { screen: "home" | "catalog" | "profile" }
  | { screen: "event"; id: string };
function readRoute(): Route {
  const path = window.location.pathname;
  if (path === "/catalog") return { screen: "catalog" };
  if (path === "/profile") return { screen: "profile" };
  if (path.startsWith("/events/")) {
    try {
      return { screen: "event", id: decodeURIComponent(path.slice(8)) };
    } catch {
      /* invalid link */
    }
  }
  return { screen: "home" };
}
function pathFor(route: Route) {
  return route.screen === "home"
    ? "/"
    : route.screen === "event"
      ? `/events/${encodeURIComponent(route.id)}`
      : `/${route.screen}`;
}
export function useNavigation() {
  const [route, setRoute] = useState<Route>(readRoute);
  useEffect(() => {
    if (!history.state?.radar)
      history.replaceState({ radar: true, depth: 0 }, "", pathFor(readRoute()));
    const onPop = () => setRoute(readRoute());
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);
  const navigate = useCallback((next: Route, replace = false) => {
    if (window.location.pathname === pathFor(next)) return;
    const depth = Number(history.state?.depth || 0) + (replace ? 0 : 1);
    history[replace ? "replaceState" : "pushState"](
      { radar: true, depth },
      "",
      pathFor(next),
    );
    setRoute(next);
  }, []);
  const back = useCallback(() => {
    if (history.state?.radar && history.state.depth > 0) history.back();
    else
      navigate(
        { screen: readRoute().screen === "event" ? "catalog" : "home" },
        true,
      );
  }, [navigate]);
  return { route, navigate, back };
}
