import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: { host: "127.0.0.1", port: 5174, strictPort: true },
  build: {
    // The landing page plus standalone pages that open in their own tab.
    // Paths are relative to the project root (where vite runs).
    rollupOptions: {
      input: {
        main: "index.html",
        privacy: "privacy.html",
        terms: "terms.html",
        contact: "contact.html",
      },
    },
  },
});
