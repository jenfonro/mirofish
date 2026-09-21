import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  fullyParallel: true,
  workers: 2,
  reporter: "list",
  use: {
    baseURL: "http://console.test",
    viewport: { width: 1440, height: 1000 },
    timezoneId: "UTC",
    screenshot: "only-on-failure",
  },
});
