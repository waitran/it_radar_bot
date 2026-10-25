import { useState } from "react";
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
import { Catalog } from "../screens/Catalog";
import { defaultFilters, type Filters } from "../types";
import { event, result } from "./fixtures";
vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  api: { events: vi.fn() },
}));

function Harness() {
  const [filters, setFilters] = useState<Filters>(defaultFilters);
  const [page, setPage] = useState(1);
  return (
    <Catalog
      filters={filters}
      page={page}
      onChange={(value) => {
        setFilters(value);
        setPage(1);
      }}
      onPage={setPage}
      onEvent={vi.fn()}
    />
  );
}
beforeEach(() => {
  vi.mocked(api.events).mockReset().mockResolvedValue(result);
});

describe("Каталог", () => {
  it("loads actual API data and renders cards and count", async () => {
    render(<Harness />);
    expect(
      screen.getByRole("status", { name: "Загрузка возможностей" }),
    ).toBeInTheDocument();
    expect(await screen.findByText(event.title)).toBeInTheDocument();
    expect(screen.getByText("Найдено: 1")).toBeInTheDocument();
    expect(api.events).toHaveBeenCalledWith(
      defaultFilters,
      1,
      expect.any(AbortSignal),
    );
  });
  it("debounces search and sends the final query", async () => {
    vi.useFakeTimers();
    render(<Harness />);
    await act(() => vi.advanceTimersByTimeAsync(0));
    const input = screen.getByRole("searchbox");
    fireEvent.change(input, { target: { value: "Py" } });
    await act(() => vi.advanceTimersByTimeAsync(250));
    fireEvent.change(input, { target: { value: "Python" } });
    await act(() => vi.advanceTimersByTimeAsync(399));
    expect(api.events).toHaveBeenCalledTimes(1);
    await act(() => vi.advanceTimersByTimeAsync(1));
    expect(api.events).toHaveBeenLastCalledWith(
      { ...defaultFilters, q: "Python" },
      1,
      expect.any(AbortSignal),
    );
  });
  it("supports categories, the actual active_only filter and publication sort", async () => {
    render(<Harness />);
    await screen.findByText(event.title);
    await userEvent.click(screen.getByRole("button", { name: "Конкурсы" }));
    await waitFor(() =>
      expect(api.events).toHaveBeenLastCalledWith(
        { ...defaultFilters, category: "contests" },
        1,
        expect.any(AbortSignal),
      ),
    );
    await userEvent.click(
      screen.getByRole("checkbox", { name: "Только актуальные" }),
    );
    await userEvent.selectOptions(
      screen.getByLabelText("Сортировка публикаций"),
      "asc",
    );
    await waitFor(() =>
      expect(api.events).toHaveBeenLastCalledWith(
        {
          ...defaultFilters,
          category: "contests",
          active_only: true,
          sort: "asc",
        },
        1,
        expect.any(AbortSignal),
      ),
    );
  });
  it("paginates on the server and resets page when a category changes", async () => {
    vi.mocked(api.events).mockResolvedValue({ ...result, total: 25 });
    render(<Harness />);
    await screen.findByText(event.title);
    expect(
      screen.getByRole("button", { name: "Предыдущая страница" }),
    ).toBeDisabled();
    await userEvent.click(
      screen.getByRole("button", { name: "Следующая страница" }),
    );
    await waitFor(() =>
      expect(api.events).toHaveBeenLastCalledWith(
        defaultFilters,
        2,
        expect.any(AbortSignal),
      ),
    );
    await userEvent.click(screen.getByRole("button", { name: "Стажировки" }));
    await waitFor(() =>
      expect(api.events).toHaveBeenLastCalledWith(
        { ...defaultFilters, category: "internships" },
        1,
        expect.any(AbortSignal),
      ),
    );
  });
  it("shows empty results and resets filters", async () => {
    vi.mocked(api.events).mockResolvedValue({ ...result, items: [], total: 0 });
    render(<Harness />);
    expect(
      await screen.findByText("Новые возможности в пути"),
    ).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Карьера" }));
    expect(
      await screen.findByText("Пока ничего не нашлось"),
    ).toBeInTheDocument();
    await userEvent.click(
      screen.getByRole("button", { name: "Сбросить фильтры" }),
    );
    await waitFor(() =>
      expect(api.events).toHaveBeenLastCalledWith(
        defaultFilters,
        1,
        expect.any(AbortSignal),
      ),
    );
  });
  it("shows safe API errors and retries on demand", async () => {
    vi.mocked(api.events)
      .mockRejectedValueOnce(new ApiError(503, "/private/db/path"))
      .mockResolvedValue(result);
    render(<Harness />);
    expect(await screen.findByRole("alert")).not.toHaveTextContent(
      "/private/db/path",
    );
    await userEvent.click(
      screen.getByRole("button", { name: "Повторить загрузку" }),
    );
    expect(await screen.findByText(event.title)).toBeInTheDocument();
    expect(api.events).toHaveBeenCalledTimes(2);
  });
  it("does not show stale responses after rapid category changes", async () => {
    let finish!: (value: typeof result) => void;
    vi.mocked(api.events)
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            finish = resolve;
          }),
      )
      .mockResolvedValue({
        ...result,
        items: [{ ...event, title: "Новая категория" }],
      });
    render(<Harness />);
    await userEvent.click(screen.getByRole("button", { name: "Конкурсы" }));
    expect(await screen.findByText("Новая категория")).toBeInTheDocument();
    await act(async () => finish(result));
    expect(screen.queryByText(event.title)).not.toBeInTheDocument();
  });
});
