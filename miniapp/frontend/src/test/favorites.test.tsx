import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "../api";
import { App } from "../App";
import { FavoritesProvider } from "../favorites";
import { FavoriteButton } from "../components/FavoriteButton";
import { Favorites } from "../screens/Favorites";
import { event, result, user } from "./fixtures";
import type { FavoritePage, FavoriteState } from "../types";
vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  api: {
    me: vi.fn(),
    events: vi.fn(),
    event: vi.fn(),
    favorites: vi.fn(),
    setFavorite: vi.fn(),
  },
}));
vi.mock("../telegram", () => ({
  getInitData: () => "synthetic-init-data",
  configureBackButton: () => vi.fn(),
  openInTelegram: () => false,
  botUrl: () => "https://t.me/test_radar_bot",
}));
const page: FavoritePage = {
  items: [
    {
      item_key: event.id,
      created_at: "2026-10-25T10:00:00Z",
      event: { ...event, is_favorite: true },
    },
  ],
  total: 1,
  page: 1,
  page_size: 12,
};
function List() {
  return (
    <FavoritesProvider userId={42}>
      <Favorites
        page={1}
        onPage={vi.fn()}
        onEvent={vi.fn()}
        onCatalog={vi.fn()}
      />
    </FavoritesProvider>
  );
}
beforeEach(() => {
  history.replaceState(null, "", "/");
  vi.mocked(api.me).mockReset().mockResolvedValue(user);
  vi.mocked(api.events).mockReset().mockResolvedValue(result);
  vi.mocked(api.event).mockReset().mockResolvedValue(event);
  vi.mocked(api.favorites).mockReset().mockResolvedValue(page);
  vi.mocked(api.setFavorite)
    .mockReset()
    .mockResolvedValue({ id: event.id, is_favorite: true });
});

describe("Server-backed favorites", () => {
  it("waits for confirmation and prevents repeated clicks even on another button for the same item", async () => {
    let finish!: (value: FavoriteState) => void;
    vi.mocked(api.setFavorite).mockImplementation(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    render(
      <FavoritesProvider userId={42}>
        <FavoriteButton id={event.id} saved={false} />
        <FavoriteButton id={event.id} saved={false} compact />
      </FavoritesProvider>,
    );
    const buttons = screen.getAllByRole("button", {
      name: "Сохранить в избранное",
    });
    fireEvent.click(buttons[0]);
    fireEvent.click(buttons[0]);
    fireEvent.click(buttons[1]);
    expect(api.setFavorite).toHaveBeenCalledTimes(1);
    expect(buttons[0]).toBeDisabled();
    expect(buttons[1]).toBeDisabled();
    expect(buttons[0]).toHaveAttribute("aria-pressed", "false");
    await act(async () => finish({ id: event.id, is_favorite: true }));
    expect(
      screen.getAllByRole("button", { name: "Удалить из избранного" }),
    ).toHaveLength(2);
  });
  it("keeps the old state on errors and retries the same idempotent operation", async () => {
    vi.mocked(api.setFavorite)
      .mockRejectedValueOnce(new Error("private/path"))
      .mockResolvedValue({ id: event.id, is_favorite: true });
    render(
      <FavoritesProvider userId={42}>
        <FavoriteButton id={event.id} saved={false} />
      </FavoritesProvider>,
    );
    await userEvent.click(
      screen.getByRole("button", { name: "Сохранить в избранное" }),
    );
    expect(await screen.findByRole("alert")).not.toHaveTextContent(
      "private/path",
    );
    await userEvent.click(
      screen.getByRole("button", { name: "Сохранить в избранное" }),
    );
    expect(
      await screen.findByRole("button", { name: "Удалить из избранного" }),
    ).toHaveAttribute("aria-pressed", "true");
    expect(
      vi.mocked(api.setFavorite).mock.calls.map((call) => call.slice(0, 2)),
    ).toEqual([
      [event.id, true],
      [event.id, true],
    ]);
  });
  it("renders saved cards, original links and removes a confirmed deletion", async () => {
    vi.mocked(api.favorites)
      .mockResolvedValueOnce(page)
      .mockResolvedValue({ ...page, items: [], total: 0 });
    vi.mocked(api.setFavorite).mockResolvedValue({
      id: event.id,
      is_favorite: false,
    });
    render(<List />);
    await screen.findByText(event.title);
    expect(
      screen.getByRole("link", { name: "Открыть оригинал" }),
    ).toHaveAttribute("href", event.url);
    await userEvent.click(
      screen.getByRole("button", { name: "Удалить из избранного" }),
    );
    expect(
      await screen.findByText("Здесь будут твои находки"),
    ).toBeInTheDocument();
    expect(api.setFavorite).toHaveBeenCalledWith(
      event.id,
      false,
      expect.any(AbortSignal),
    );
  });
  it("lets a user delete an unavailable event without inventing its data", async () => {
    vi.mocked(api.favorites).mockResolvedValue({
      ...page,
      items: [{ ...page.items[0], event: null }],
    });
    render(<List />);
    expect(
      await screen.findByText("Публикация больше недоступна"),
    ).toBeInTheDocument();
    expect(screen.queryByText(event.title)).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Удалить из избранного" }),
    ).toBeEnabled();
  });
  it("shows empty favorites, API errors and retry", async () => {
    vi.mocked(api.favorites)
      .mockRejectedValueOnce(new ApiError(503, "private"))
      .mockResolvedValue({ ...page, total: 0, items: [] });
    render(<List />);
    await screen.findByRole("alert");
    await userEvent.click(
      screen.getByRole("button", { name: "Повторить загрузку" }),
    );
    expect(
      await screen.findByText("Здесь будут твои находки"),
    ).toBeInTheDocument();
  });
  it("does not share mutation state between verified users", async () => {
    const view = render(
      <FavoritesProvider userId={42}>
        <FavoriteButton id={event.id} saved={false} />
      </FavoritesProvider>,
    );
    await userEvent.click(
      screen.getByRole("button", { name: "Сохранить в избранное" }),
    );
    await screen.findByRole("button", { name: "Удалить из избранного" });
    view.rerender(
      <FavoritesProvider userId={99}>
        <FavoriteButton id={event.id} saved={false} />
      </FavoritesProvider>,
    );
    expect(
      screen.getByRole("button", { name: "Сохранить в избранное" }),
    ).toHaveAttribute("aria-pressed", "false");
  });
  it("synchronizes catalog and detail buttons and returns to favorites", async () => {
    render(<App />);
    await screen.findByText(event.title);
    await userEvent.click(
      screen.getByRole("button", { name: "Сохранить в избранное" }),
    );
    await screen.findByRole("button", { name: "Удалить из избранного" });
    await userEvent.click(screen.getByRole("button", { name: "Избранное" }));
    await screen.findByRole("heading", { name: /^Избранное/ });
    await userEvent.click(
      await screen.findByRole("button", { name: /Тестовый Python-митап/ }),
    );
    await screen.findByRole("heading", { name: event.title });
    expect(
      screen.getByRole("button", { name: "Удалить из избранного" }),
    ).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "Избранное" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    await userEvent.click(screen.getByRole("button", { name: "Назад" }));
    expect(
      await screen.findByRole("heading", { name: /^Избранное/ }),
    ).toBeInTheDocument();
  });
});
