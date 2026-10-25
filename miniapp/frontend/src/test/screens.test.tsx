import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api";
import { App } from "../App";
import { EventCard } from "../components/EventCard";
import { Detail } from "../screens/Detail";
import { Home } from "../screens/Home";
import { Profile } from "../screens/Profile";
import { configureBackButton, getInitData } from "../telegram";
import { event, result, user } from "./fixtures";
vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  api: { me: vi.fn(), events: vi.fn(), event: vi.fn() },
}));
vi.mock("../telegram", () => ({
  getInitData: vi.fn(() => "synthetic-init-data"),
  configureBackButton: vi.fn(() => vi.fn()),
  openInTelegram: vi.fn(() => false),
  botUrl: () => "https://t.me/test_radar_bot",
}));
beforeEach(() => {
  history.replaceState(null, "", "/");
  vi.mocked(getInitData).mockReturnValue("synthetic-init-data");
  vi.mocked(api.me).mockReset().mockResolvedValue(user);
  vi.mocked(api.events).mockReset().mockResolvedValue(result);
  vi.mocked(api.event).mockReset().mockResolvedValue(event);
});

describe("Карточки и экраны", () => {
  it("card opens the existing item key and escapes HTML as text", async () => {
    const open = vi.fn();
    const title = "<img src=x onerror=alert(1)>";
    const { container } = render(
      <EventCard
        event={{ ...event, title, summary: "<script>test</script>" }}
        onOpen={open}
      />,
    );
    expect(container.querySelector("img, script")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: /<img/ }));
    expect(open).toHaveBeenCalledWith(event.id);
    expect(screen.getByText("@test_channel")).toBeInTheDocument();
  });
  it("detail separates unknown dates and does not invent registration status", async () => {
    render(<Detail id={event.id} onBack={vi.fn()} />);
    await screen.findByRole("heading", { name: event.title });
    expect(screen.getByText("Дата неизвестна")).toBeInTheDocument();
    expect(
      screen.getByText(/Статус регистрации не подтверждён/),
    ).toBeInTheDocument();
    expect(screen.queryByText("Приём заявок до")).not.toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Открыть регистрацию" }),
    ).toHaveAttribute("href", event.apply_url);
    expect(
      screen.getByRole("link", { name: "Открыть оригинал" }),
    ).toHaveAttribute("href", event.url);
  });
  it("detail never turns an unsafe API link into a clickable URL", async () => {
    vi.mocked(api.event).mockResolvedValue({
      ...event,
      apply_url: "javascript:alert(1)",
      url: "file:///private/bot.db",
    });
    render(<Detail id={event.id} onBack={vi.fn()} />);
    await screen.findByRole("heading", { name: event.title });
    expect(screen.queryAllByRole("link")).toHaveLength(0);
  });
  it("shows deadline block only for reliable, future, nonclosed deadlines", async () => {
    const future = new Date(Date.now() + 86400000).toISOString();
    vi.mocked(api.events).mockResolvedValue({
      ...result,
      items: [{ ...event, deadline_at: future, deadline_precision: "date" }],
    });
    render(<Home user={user} onCatalog={vi.fn()} onEvent={vi.fn()} />);
    expect(
      await screen.findByRole("heading", { name: "Ближайшие дедлайны" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Среди последних 1 публикаций/),
    ).toBeInTheDocument();
  });
  it("home has no fictional deadlines when dates are absent", async () => {
    render(<Home user={user} onCatalog={vi.fn()} onEvent={vi.fn()} />);
    await screen.findByText(event.title);
    expect(
      screen.queryByRole("heading", { name: "Ближайшие дедлайны" }),
    ).not.toBeInTheDocument();
  });
  it("profile renders verified /me data and bot link", () => {
    render(<Profile user={user} />);
    expect(screen.getByText("@anna_test")).toBeInTheDocument();
    expect(screen.getByText("42")).toBeInTheDocument();
    expect(screen.getByText("Подтверждено Telegram")).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /IT Radar в Telegram/ }),
    ).toHaveAttribute("href", "https://t.me/test_radar_bot");
  });
  it("navigates home → category → detail → back → profile", async () => {
    render(<App />);
    await screen.findByRole("heading", { name: "Новые возможности" });
    await userEvent.click(screen.getByRole("button", { name: "Конкурсы" }));
    await screen.findByRole("heading", { name: /Каталог возможностей/ });
    expect(screen.getByRole("button", { name: "Конкурсы" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await userEvent.click(
      await screen.findByRole("button", { name: /Тестовый Python-митап/ }),
    );
    await screen.findByRole("heading", { name: event.title });
    expect(location.pathname).toBe("/events/test%3A1");
    await userEvent.click(screen.getByRole("button", { name: "Назад" }));
    await screen.findByRole("heading", { name: /Каталог возможностей/ });
    expect(screen.getByRole("button", { name: "Конкурсы" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await userEvent.click(screen.getByRole("button", { name: "Профиль" }));
    expect(await screen.findByText("42")).toBeInTheDocument();
    expect(configureBackButton).toHaveBeenCalledWith(
      true,
      expect.any(Function),
    );
  });
  it("handles Telegram back callback and browser popstate", async () => {
    render(<App />);
    await screen.findByRole("heading", { name: "Новые возможности" });
    await userEvent.click(screen.getByRole("button", { name: "Каталог" }));
    await screen.findByRole("heading", { name: /Каталог возможностей/ });
    const calls = vi.mocked(configureBackButton).mock.calls;
    const back = calls[calls.length - 1][1];
    act(() => back());
    await waitFor(() => expect(location.pathname).toBe("/"));
    expect(
      await screen.findByRole("heading", { name: "Новые возможности" }),
    ).toBeInTheDocument();
  });
  it("never sends API requests or displays a fabricated user outside Telegram", async () => {
    vi.mocked(getInitData).mockReturnValue("");
    render(<App />);
    await screen.findByText("Безопасный вход через Telegram");
    expect(api.me).not.toHaveBeenCalled();
    expect(api.events).not.toHaveBeenCalled();
    expect(
      screen.queryByRole("navigation", { name: "Основная навигация" }),
    ).not.toBeInTheDocument();
  });
});
