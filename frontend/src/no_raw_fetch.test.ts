// Every request carries an AbortSignal so a superseded or unmounted caller cannot write state late. The one raw fetch
// lives in request.ts; everything else goes through it.
import { expect, test } from "vitest";

const ALLOWED = "request.ts";

// Also catches `fetch (`, `window.fetch(`, `globalThis.fetch(`, a line break before the parenthesis, and XMLHttpRequest.
function rawFetches(files: Record<string, string>): string[] {
  return Object.entries(files)
    .filter(([file]) => file !== ALLOWED && !/\.test\.tsx?$/.test(file))
    .flatMap(([file, text]) =>
      [...text.matchAll(/\bfetch\s*\(|\bXMLHttpRequest\b/g)].map(
        (m) =>
          `raw request in frontend/src/${file}:${text.slice(0, m.index).split("\n").length}: call the API through fetchWithSignal in frontend/src/request.ts (or the adminApi helpers built on it) and run it with useRequest("submit"|"load") or pass an AbortSignal; see frontend/src/useRequest.ts`,
      ),
    );
}

test("no raw fetch or XMLHttpRequest outside request.ts", () => {
  const sources = import.meta.glob<string>("./**/*.{ts,tsx}", { query: "?raw", import: "default", eager: true });
  const files = Object.fromEntries(Object.entries(sources).map(([path, text]) => [path.slice(2), text]));
  expect(rawFetches(files)).toEqual([]);
});

test("the scan sees spaced, prefixed, multi-line and XMLHttpRequest calls and reports their line", () => {
  const found = rawFetches({ "A.tsx": "a\nfetch (x)\nwindow.fetch(y)\nglobalThis.fetch\n(z)\nnew XMLHttpRequest()", "request.ts": "fetch(u)", "B.test.tsx": "fetch(v)" });
  expect(found.map((f) => f.split(": call")[0])).toEqual([
    "raw request in frontend/src/A.tsx:2",
    "raw request in frontend/src/A.tsx:3",
    "raw request in frontend/src/A.tsx:4",
    "raw request in frontend/src/A.tsx:6",
  ]);
});
