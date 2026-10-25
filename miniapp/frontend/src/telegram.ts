import {
  backButton,
  init,
  miniApp,
  openLink,
  openTelegramLink,
  retrieveRawInitData,
  themeParams,
  viewport,
} from "@tma.js/sdk";

let rawInitData = "";
let inTelegram = false;
let initialized = false;

export function applyTheme(isDark: boolean) {
  document.documentElement.dataset.theme = isDark ? "dark" : "light";
  document.documentElement.style.colorScheme = isDark ? "dark" : "light";
}
export function initializeTelegram() {
  if (initialized) return;
  initialized = true;
  const media = window.matchMedia("(prefers-color-scheme: dark)");
  applyTheme(media.matches);
  media.addEventListener("change", (event) => {
    if (!inTelegram) applyTheme(event.matches);
  });
  try {
    rawInitData = retrieveRawInitData() || "";
    if (!rawInitData) return;
    init({ acceptCustomStyles: false });
    inTelegram = true;
    themeParams.mount();
    themeParams.bindCssVars();
    applyTheme(themeParams.isDark());
    themeParams.isDark.sub(applyTheme);
    if (backButton.isSupported()) backButton.mount();
    miniApp.ready();
    viewport.expand();
    viewport
      .mount({ timeout: 2500 })
      .then(() => viewport.bindCssVars())
      .catch(() => {
        // Older clients may not answer viewport requests. CSS env() remains a fallback.
      });
  } catch {
    // Browser launch gets a clear authorization screen; never synthesize a user or session.
  }
}
export const getInitData = () => rawInitData;
export const isTelegram = () => inTelegram;
export function configureBackButton(visible: boolean, handler: () => void) {
  if (!inTelegram || !backButton.isMounted()) return () => {};
  if (visible) backButton.show();
  else backButton.hide();
  const off = backButton.onClick(handler);
  return () => {
    off();
    backButton.hide();
  };
}
export function openInTelegram(url: string): boolean {
  if (!inTelegram) return false;
  try {
    if (new URL(url).hostname === "t.me" && openTelegramLink.isAvailable())
      openTelegramLink(url);
    else if (openLink.isAvailable()) openLink(url);
    else return false;
    return true;
  } catch {
    return false;
  }
}
export function botUrl(): string | null {
  const username = import.meta.env.VITE_BOT_USERNAME ?? "it_start_radar_bot";
  return /^[a-zA-Z][a-zA-Z0-9_]{4,31}$/.test(username)
    ? `https://t.me/${username}`
    : null;
}
