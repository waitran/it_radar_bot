import { useEffect, useState } from "react";
import {
  ArrowUpRight,
  Bookmark,
  Compass,
  House,
  Radar,
  ShieldCheck,
  UserRound,
} from "lucide-react";
import { api } from "./api";
import { useResource } from "./hooks";
import { botUrl, configureBackButton, getInitData } from "./telegram";
import { useNavigation } from "./navigation";
import { defaultFilters, type Category, type Filters } from "./types";
import { ErrorState, ExternalLink, Skeletons } from "./components/common";
import { Home } from "./screens/Home";
import { Catalog } from "./screens/Catalog";
import { Detail } from "./screens/Detail";
import { FavoritesProvider } from "./favorites";
import { Favorites } from "./screens/Favorites";
import { Profile } from "./screens/Profile";

export function App() {
  const { route, navigate, back } = useNavigation();
  const [filters, setFilters] = useState<Filters>(defaultFilters);
  const [page, setPage] = useState(1);
  const [favoritePage, setFavoritePage] = useState(1);
  const session = Boolean(getInitData());
  const user = useResource(
    (signal) => (session ? api.me(signal) : Promise.resolve(null)),
    `me-${session}`,
  );
  useEffect(
    () => configureBackButton(route.screen !== "home", back),
    [route.screen, back],
  );
  useEffect(() => {
    window.scrollTo({ top: 0, behavior: "instant" });
  }, [
    route.screen,
    route.screen === "event" ? route.id : "",
    page,
    favoritePage,
  ]);
  const onEvent = (id: string) =>
    navigate({
      screen: "event",
      id,
      from:
        route.screen === "favorites"
          ? "favorites"
          : route.screen === "home"
            ? "home"
            : "catalog",
    });
  const onCatalog = (q = "", category: Category | "" = "") => {
    setFilters({ ...defaultFilters, q, category });
    setPage(1);
    navigate({ screen: "catalog" });
  };
  const activeScreen =
    route.screen === "event" ? route.from || "catalog" : route.screen;
  const bot = botUrl();
  return (
    <FavoritesProvider userId={user.data?.id}>
      <div className="app-shell">
        <header className="app-header">
          <button
            className="brand"
            onClick={() => navigate({ screen: "home" })}
            aria-label="IT Radar — главная"
          >
            <span className="brand-symbol">
              <Radar size={23} strokeWidth={1.6} />
            </span>
            <span>
              IT RADAR<span className="brand-caption">ВОЗМОЖНОСТИ РЯДОМ</span>
            </span>
          </button>
          <span className="header-label">
            <span className="live-dot" />
            На твоей волне
          </span>
          {user.data && (
            <button
              className="header-avatar"
              aria-label="Открыть профиль"
              onClick={() => navigate({ screen: "profile" })}
            >
              {user.data.first_name.slice(0, 1).toUpperCase()}
            </button>
          )}
        </header>
        <main id="main-content">
          {!session ? (
            <div className="screen welcome-screen">
              <div className="welcome-art" aria-hidden="true">
                <Radar size={76} strokeWidth={1} />
              </div>
              <p className="eyebrow">ТВОЙ СЛЕДУЮЩИЙ ШАГ В IT</p>
              <h1>
                Возможности
                <br />
                начинаются здесь<span className="heading-dot">.</span>
              </h1>
              <p>
                Откройте IT Radar внутри Telegram,
                <br />
                чтобы найти стажировку, событие или конкурс.
              </p>
              {bot && (
                <ExternalLink href={bot} className="button primary">
                  Открыть в Telegram
                  <ArrowUpRight size={18} />
                </ExternalLink>
              )}
              <span className="welcome-trust">
                <ShieldCheck size={16} />
                Безопасный вход через Telegram
              </span>
            </div>
          ) : user.loading ? (
            <div className="screen">
              <Skeletons />
            </div>
          ) : user.error ? (
            <div className="screen">
              <ErrorState error={user.error} retry={user.retry} />
            </div>
          ) : user.data ? (
            route.screen === "home" ? (
              <Home user={user.data} onCatalog={onCatalog} onEvent={onEvent} />
            ) : route.screen === "catalog" ? (
              <Catalog
                filters={filters}
                page={page}
                onChange={(next) => {
                  setFilters(next);
                  setPage(1);
                }}
                onPage={setPage}
                onEvent={onEvent}
              />
            ) : route.screen === "favorites" ? (
              <Favorites
                page={favoritePage}
                onPage={setFavoritePage}
                onEvent={onEvent}
                onCatalog={() => onCatalog()}
              />
            ) : route.screen === "event" ? (
              <Detail id={route.id} onBack={back} />
            ) : (
              <Profile user={user.data} />
            )
          ) : null}
        </main>
        {user.data && (
          <nav className="bottom-nav" aria-label="Основная навигация">
            {(
              [
                { screen: "home", label: "Главная", Icon: House },
                { screen: "catalog", label: "Каталог", Icon: Compass },
                { screen: "favorites", label: "Избранное", Icon: Bookmark },
                { screen: "profile", label: "Профиль", Icon: UserRound },
              ] as const
            ).map(({ screen, label, Icon }) => (
              <button
                key={screen}
                aria-current={activeScreen === screen ? "page" : undefined}
                onClick={() => navigate({ screen })}
              >
                <span className="nav-icon">
                  <Icon
                    size={22}
                    strokeWidth={activeScreen === screen ? 2 : 1.7}
                  />
                </span>
                <span>{label}</span>
                {activeScreen === screen && <i className="nav-dot" />}
              </button>
            ))}
          </nav>
        )}
      </div>
    </FavoritesProvider>
  );
}
