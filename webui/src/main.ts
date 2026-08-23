import { createApp } from "vue";
import App from "./App.vue";
import "./style.css";
import "./miku.css";

const THEME_STORAGE = "mf_theme";
const SKIN_STORAGE = "mf_skin";

export function applyTheme(theme: string): void {
  if (theme === "light" || theme === "dark") {
    document.documentElement.setAttribute("data-theme", theme);
  } else {
    document.documentElement.removeAttribute("data-theme");
  }
}

export function storedTheme(): string {
  try {
    return localStorage.getItem(THEME_STORAGE) || "system";
  } catch {
    return "system";
  }
}

export function saveTheme(theme: string): void {
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
    return localStorage.getItem(SKIN_STORAGE) || "miku";
  } catch {
    return "miku";
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

applyTheme(storedTheme());
applySkin(storedSkin());
createApp(App).mount("#app");
