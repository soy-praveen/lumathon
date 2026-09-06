import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const API_TARGET = "http://127.0.0.1:8000";
const API_PATHS = ["/health", "/close", "/jes", "/evidence", "/exceptions", "/rules", "/metrics"];

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: Object.fromEntries(API_PATHS.map((path) => [path, API_TARGET])),
  },
});
