import { createApp } from "vue";
import App from "./App.vue";
import "./style.css";

const THEME_STORAGE = "mf_theme";

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

applyTheme(storedTheme());
createApp(App).mount("#app");
