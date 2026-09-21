import { createApp } from "vue";
import App from "./App.vue";
import "./style.css";

const THEME_STORAGE = "mf_theme";
const SKIN_STORAGE = "mf_skin";
const PAGE_STORAGE = "mf_page";

export type ThemeMode = "system" | "light" | "dark";
export type PageId =
  | "overview"
  | "accounts"
  | "usage"
  | "network"
  | "playground"
  | "appeal"
  | "settings";

export function applyTheme(theme: ThemeMode): void {
  if (theme === "light" || theme === "dark") {
    document.documentElement.setAttribute("data-theme", theme);
  } else {
    document.documentElement.removeAttribute("data-theme");
  }
}

export function storedTheme(): ThemeMode {
  try {
    const raw = localStorage.getItem(THEME_STORAGE);
    return raw === "light" || raw === "dark" || raw === "system" ? raw : "system";
  } catch {
    return "system";
  }
}

export function saveTheme(theme: ThemeMode): void {
  try {
    localStorage.setItem(THEME_STORAGE, theme);
  } catch {
    /* storage may be blocked; theme then resets per session */
  }
  applyTheme(theme);
}

export function applySkin(skin: string): void {
  if (skin === "miku") {
    document.documentElement.setAttribute("data-skin", "miku");
  } else {
    document.documentElement.removeAttribute("data-skin");
  }
}

export function storedSkin(): string {
  try {
    return localStorage.getItem(SKIN_STORAGE) || "plain";
  } catch {
    return "plain";
  }
}

export function saveSkin(skin: string): void {
  try {
    localStorage.setItem(SKIN_STORAGE, skin);
  } catch {
    /* storage may be blocked; skin then resets per session */
  }
  applySkin(skin);
}

export function storedPage(): PageId {
  try {
    const raw = localStorage.getItem(PAGE_STORAGE) as PageId | null;
    const pages: PageId[] = [
      "overview", "accounts", "usage", "network",
      "playground", "appeal", "settings",
    ];
    return raw && pages.includes(raw) ? raw : "overview";
  } catch {
    return "overview";
  }
}

export function savePage(page: PageId): void {
  try {
    localStorage.setItem(PAGE_STORAGE, page);
  } catch {
    /* ignore */
  }
}

applyTheme(storedTheme());
applySkin(storedSkin());
createApp(App).mount("#app");
