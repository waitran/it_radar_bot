import { beforeEach, describe, expect, it, vi } from "vitest";
const telegram = vi.hoisted(() => ({ getInitData: vi.fn() }));
vi.mock("../telegram", () => telegram);
let client: typeof import("../api");

beforeEach(async () => {
  vi.resetModules();
  vi.useFakeTimers();
  telegram.getInitData.mockReturnValue("test-signed-raw-query");
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({ ok: true, json: async () => ({ id: 42 }) }),
  );
  client = await import("../api");
});

describe("API transport", () => {
  it("sends unchanged raw initData in Authorization header to the same origin", async () => {
    const promise = client.api.me(new AbortController().signal);
    await vi.runAllTimersAsync();
    await promise;
    expect(fetch).toHaveBeenCalledWith(
      "/api/me",
      expect.objectContaining({
        headers: {
          Authorization: "tma test-signed-raw-query",
          Accept: "application/json",
        },
        cache: "no-store",
        credentials: "omit",
        redirect: "error",
      }),
    );
  });
  it("uses only supported query filters and URL-encodes item identifiers", async () => {
    const promise = client.api.events(
      {
        q: "SQL & Python",
        category: "contests",
        active_only: true,
        sort: "asc",
      },
      2,
      new AbortController().signal,
    );
    await vi.runAllTimersAsync();
    await promise;
    const url = new URL(
      vi.mocked(fetch).mock.calls[0][0] as string,
      "https://example.org",
    );
    expect(url.searchParams.get("q")).toBe("SQL & Python");
    expect(url.searchParams.get("page")).toBe("2");
    expect(url.searchParams.get("page_size")).toBe("12");
    const detail = client.api.event("source:a/b", new AbortController().signal);
    await vi.runAllTimersAsync();
    await detail;
    expect(fetch).toHaveBeenLastCalledWith(
      "/api/events/source%3Aa%2Fb",
      expect.anything(),
    );
  });
  it("uses authenticated idempotent PUT and DELETE for favorites", async () => {
    const saved = client.api.setFavorite(
      "source:a/b",
      true,
      new AbortController().signal,
    );
    await vi.runAllTimersAsync();
    await saved;
    expect(fetch).toHaveBeenLastCalledWith(
      "/api/favorites/source%3Aa%2Fb",
      expect.objectContaining({ method: "PUT" }),
    );
    const removed = client.api.setFavorite(
      "source:a/b",
      false,
      new AbortController().signal,
    );
    await vi.runAllTimersAsync();
    await removed;
    expect(fetch).toHaveBeenLastCalledWith(
      "/api/favorites/source%3Aa%2Fb",
      expect.objectContaining({ method: "DELETE" }),
    );
  });
  it("limits request starts to 250ms intervals", async () => {
    const times: number[] = [];
    vi.mocked(fetch).mockImplementation(async () => {
      times.push(Date.now());
      return { ok: true, json: async () => ({}) } as Response;
    });
    const pending = [1, 2, 3].map(() =>
      client.api.me(new AbortController().signal),
    );
    await vi.runAllTimersAsync();
    await Promise.all(pending);
    expect(times[1] - times[0]).toBeGreaterThanOrEqual(250);
    expect(times[2] - times[1]).toBeGreaterThanOrEqual(250);
  });
  it("cancels queued obsolete requests before hitting the server", async () => {
    const controller = new AbortController();
    const promise = client.api.me(controller.signal);
    const rejection = expect(promise).rejects.toMatchObject({
      name: "AbortError",
    });
    controller.abort();
    await vi.runAllTimersAsync();
    await rejection;
    expect(fetch).not.toHaveBeenCalled();
  });
  it("rejects missing authorization and never leaks the server response body", async () => {
    telegram.getInitData.mockReturnValue("");
    await expect(
      client.api.me(new AbortController().signal),
    ).rejects.toMatchObject({ status: 401 });
    expect(fetch).not.toHaveBeenCalled();
    telegram.getInitData.mockReturnValue("test");
    vi.mocked(fetch).mockResolvedValue({
      ok: false,
      status: 503,
      text: async () => "/secret/path",
    } as Response);
    const failed = client.api.me(new AbortController().signal);
    const rejection = expect(failed).rejects.toThrow("API request failed");
    await vi.runAllTimersAsync();
    await rejection;
  });
});
