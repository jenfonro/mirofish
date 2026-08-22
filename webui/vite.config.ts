import vue from "@vitejs/plugin-vue";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [vue()],
  build: {
    outDir: "../mirofish/static",
    emptyOutDir: true,
  },
  server: {
    proxy: {
      // vite dev server proxies API calls to a locally running relay
      "^/(health|accounts|proxies|api|v1)": "http://127.0.0.1:8787",
    },
  },
});
