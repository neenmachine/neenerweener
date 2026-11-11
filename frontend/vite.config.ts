import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: {
    rolldownOptions: {
      output: {
        // Vite's dynamic-import helper in its own chunk, not in the app entry: the admin post preview frame loads
        // mermaid and its controller (previewFrame.ts) without running the app (see postPreview.ts).
        codeSplitting: { groups: [{ name: "preload-helper", test: /preload-helper/ }] },
      },
    },
  },
});
