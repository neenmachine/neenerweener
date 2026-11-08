import { useEffect, useRef, useState } from "react";

// The one owner of a component's request lifecycle. run(fn) calls fn with an AbortSignal for its fetch.
// - "submit": a call while one runs is dropped; busy is set by the running request and cleared only by its own settle.
// - "load": a call aborts the one before it; only the latest result is returned, and busy follows the latest.
// Unmount aborts the running request. A dropped, superseded or aborted call never settles, so the caller's code after
// `await run(...)` runs only for a result it may still apply: no setter, focus or object URL after it is moot.
export function useRequest(policy: "submit" | "load") {
  const [busy, setBusy] = useState(false);
  const running = useRef<AbortController | null>(null);
  useEffect(
    () => () => {
      running.current?.abort();
      running.current = null;
    },
    [],
  );

  const run = async <T>(fn: (signal: AbortSignal) => Promise<T>): Promise<T> => {
    if (policy === "submit" && running.current) return never();
    running.current?.abort();
    const mine = (running.current = new AbortController());
    setBusy(true);
    try {
      const result = await fn(mine.signal);
      return running.current === mine ? result : never();
    } finally {
      if (running.current === mine) {
        running.current = null;
        setBusy(false);
      }
    }
  };
  return { run, busy };
}

const never = <T>() => new Promise<T>(() => {});
