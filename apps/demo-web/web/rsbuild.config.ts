import { defineConfig } from "@rsbuild/core";
import { pluginReact } from "@rsbuild/plugin-react";

const webPort = Number(process.env.DEMO_WEB_PORT ?? 3010);
const apiUrl = process.env.DEMO_API_URL ?? "http://127.0.0.1:8001";

export default defineConfig({
  plugins: [pluginReact()],
  server: {
    port: webPort,
    proxy: {
      "/api": {
        target: apiUrl,
        changeOrigin: true,
        ws: true,
      },
    },
  },
  html: {
    title: "git-pg demo",
  },
});
