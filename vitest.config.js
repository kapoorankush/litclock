// vitest + jsdom harness for control_server static/js/* (#338).
//
// pretendToBeVisual:true is required because src/control_server/static/js/
// updates.js uses requestAnimationFrame inside openConfirmSheet() without a
// fallback. jsdom only exposes rAF when this flag is set; without it the
// dialog never reaches its open state and the optimistic-tick test for #329
// can't get past the confirm modal.
//
// isolate:true gives each test FILE a fresh jsdom (default since v0.34, pinned
// for clarity). Within a file, beforeEach rebuilds document.body so closure
// state in the IIFE-wrapped production scripts re-initializes between cases.
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "jsdom",
    environmentOptions: {
      jsdom: { pretendToBeVisual: true },
    },
    include: ["tests/js/**/*.test.js"],
    globals: false,
    isolate: true,
  },
});
