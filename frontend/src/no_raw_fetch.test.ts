// Every request carries an AbortSignal so a superseded or unmounted caller cannot write state late. Raw fetch
// lives only in the modules below; everything else goes through them.
import { expect, test } from "vitest";

const ALLOWED: Record<string, string> = {
  "adminApi.ts": "the admin request module: send() wraps fetch with the caller's signal",
  // The public pages cannot import adminApi (an admin-only lazy chunk; make chunk-guard keeps it out of the main one).
  "App.tsx": "public reads: useApi's effect controller and the timeline page's load-more controller",
};

function rawFetches(files: Record<string, string>): string[] {
  return Object.entries(files)
    .filter(([file]) => !(file in ALLOWED) && !/\.test\.tsx?$/.test(file))
    .flatMap(([file, text]) =>
      text.split("\n").flatMap((line, i) =>
        /\bfetch\(/.test(line)
          ? [`raw fetch in frontend/src/${file}:${i + 1}: call the API through adminApi/request helpers and run it with useRequest("submit"|"load") or pass an AbortSignal; see frontend/src/useRequest.ts`]
          : [],
      ),
    );
}

test("no raw fetch outside the request modules", () => {
  const sources = import.meta.glob<string>("./**/*.{ts,tsx}", { query: "?raw", import: "default", eager: true });
  const files = Object.fromEntries(Object.entries(sources).map(([path, text]) => [path.slice(2), text]));
  expect(rawFetches(files)).toEqual([]);
});
