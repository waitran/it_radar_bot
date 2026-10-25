import { beforeEach, describe, expect, it, vi } from "vitest";
const sdk = vi.hoisted(() => ({
  retrieveRawInitData: vi.fn(),
  init: vi.fn(),
  themeParams: {
    mount: vi.fn(),
    bindCssVars: vi.fn(),
    isDark: Object.assign(vi.fn(), { sub: vi.fn() }),
  },
  backButton: {
    isSupported: vi.fn(),
    mount: vi.fn(),
    isMounted: vi.fn(),
    show: vi.fn(),
    hide: vi.fn(),
    onClick: vi.fn(),
  },
  miniApp: { ready: vi.fn() },
  viewport: { expand: vi.fn(), mount: vi.fn(), bindCssVars: vi.fn() },
  openLink: Object.assign(vi.fn(), { isAvailable: vi.fn() }),
  openTelegramLink: Object.assign(vi.fn(), { isAvailable: vi.fn() }),
}));
vi.mock("@tma.js/sdk", () => sdk);

beforeEach(() => {
  vi.resetModules();
  vi.clearAllMocks();
  vi.mocked(window.matchMedia).mockImplementation((query) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }));
  document.documentElement.removeAttribute("data-theme");
  sdk.retrieveRawInitData.mockReturnValue("signed=unchanged%2Braw&hash=test");
  sdk.themeParams.isDark.mockReturnValue(false);
  sdk.backButton.isSupported.mockReturnValue(true);
  sdk.backButton.isMounted.mockReturnValue(true);
  sdk.backButton.onClick.mockReturnValue(vi.fn());
  sdk.viewport.mount.mockResolvedValue(undefined);
});

describe("Telegram SDK integration and themes", () => {
  it("binds Telegram CSS variables, applies light theme and retains raw initData", async () => {
    const bridge = await import("../telegram");
    bridge.initializeTelegram();
    expect(bridge.getInitData()).toBe("signed=unchanged%2Braw&hash=test");
    expect(sdk.init).toHaveBeenCalledWith({ acceptCustomStyles: false });
    expect(sdk.themeParams.bindCssVars).toHaveBeenCalled();
    expect(document.documentElement).toHaveAttribute("data-theme", "light");
    expect(sdk.miniApp.ready).toHaveBeenCalled();
    expect(sdk.viewport.expand).toHaveBeenCalled();
    await Promise.resolve();
    expect(sdk.viewport.bindCssVars).toHaveBeenCalled();
  });
  it("supports dark theme and live theme changes without changing identity", async () => {
    sdk.themeParams.isDark.mockReturnValue(true);
    const bridge = await import("../telegram");
    bridge.initializeTelegram();
    expect(document.documentElement).toHaveAttribute("data-theme", "dark");
    expect(document.documentElement.style.colorScheme).toBe("dark");
    const change = sdk.themeParams.isDark.sub.mock.calls[0][0];
    change(false);
    expect(document.documentElement).toHaveAttribute("data-theme", "light");
    expect(bridge.getInitData()).toContain("signed=");
  });
  it("never fabricates initData outside Telegram", async () => {
    sdk.retrieveRawInitData.mockReturnValue(undefined);
    const bridge = await import("../telegram");
    bridge.initializeTelegram();
    expect(bridge.getInitData()).toBe("");
    expect(sdk.init).not.toHaveBeenCalled();
  });
  it("subscribes and cleans up BackButton listeners", async () => {
    const bridge = await import("../telegram");
    bridge.initializeTelegram();
    const handler = vi.fn();
    const off = vi.fn();
    sdk.backButton.onClick.mockReturnValue(off);
    const cleanup = bridge.configureBackButton(true, handler);
    expect(sdk.backButton.show).toHaveBeenCalled();
    expect(sdk.backButton.onClick).toHaveBeenCalledWith(handler);
    cleanup();
    expect(off).toHaveBeenCalled();
    expect(sdk.backButton.hide).toHaveBeenCalled();
  });
});
