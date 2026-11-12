// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, expect, test, vi } from "vitest";
import CanvasPage, { applyCanvasEvents, loadCanvasHistory, type CanvasSnapshot } from "./CanvasPage";

const snapshot: CanvasSnapshot = { width: 64, height: 64, palette: ["#000000", "#ffffff"], revision: 0, pixels: "" };
let root: Root | undefined;

afterEach(() => {
  act(() => root?.unmount());
  root = undefined;
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

test("canvas events apply placements and clear regions in order", () => {
  const pixels = new Uint8Array([255, 1, 2, 3, 4, 5, 6, 7, 8]);
  expect([...applyCanvasEvents(pixels, [
    { revision: 1, event_kind: "place", x: 0, y: 0, color: 9 },
    { revision: 2, event_kind: "clear", clear_bounds: { x: 1, y: 0, width: 2, height: 2 } },
  ], 3)]).toEqual([9, 255, 255, 3, 255, 255, 6, 7, 8]);
});

test("history follows every page instead of stopping at the first 500 events", async () => {
  const pages = [
    Array.from({ length: 500 }, (_, i) => ({ revision: i + 1, event_kind: "place" as const, x: 0, y: 0, color: 1 })),
    [{ revision: 501, event_kind: "place" as const, x: 1, y: 1, color: 2 }],
  ];
  const calls: number[] = [];
  expect(typeof loadCanvasHistory).toBe("function");
  const history = await loadCanvasHistory(async (after) => {
    calls.push(after);
    return { reset: false, revision: 501, events: after === 0 ? pages[0] : pages[1] };
  }, new AbortController().signal);
  expect(history).toHaveLength(501);
  expect(calls).toEqual([0, 500]);
});

test("replay starts with an empty canvas", () => {
  const live = new Uint8Array([7, 255, 255, 255]);
  const empty = new Uint8Array(live.length).fill(255);
  expect([...applyCanvasEvents(empty, [{ revision: 1, event_kind: "place", x: 0, y: 0, color: 2 }], 2)]).toEqual([2, 255, 255, 255]);
  expect(live[0]).toBe(7);
});

test("polling pages through a full response without skipping its tail", async () => {
  vi.useFakeTimers();
  const context = { imageSmoothingEnabled: false, clearRect: vi.fn(), drawImage: vi.fn(), save: vi.fn(), restore: vi.fn(), scale: vi.fn(), createImageData: () => ({ data: new Uint8ClampedArray(64 * 64 * 4) }), putImageData: vi.fn() };
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(context as never);
  const fetch = vi.fn((url: string) => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(url.includes("after=0") ? { reset: false, revision: 2, events: [{ revision: 1, event_kind: "place", x: 0, y: 0, color: 1 }] } : { reset: false, revision: 2, events: [{ revision: 2, event_kind: "place", x: 1, y: 1, color: 1 }] }) }));
  vi.stubGlobal("fetch", fetch);
  document.body.innerHTML = '<div id="root"></div>';
  await act(async () => { root = createRoot(document.getElementById("root")!); root.render(<CanvasPage snapshot={snapshot} />); });
  await act(async () => {
    vi.advanceTimersByTime(3000);
    await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
    vi.runOnlyPendingTimers();
    await Promise.resolve(); await Promise.resolve();
  });
  expect(fetch.mock.calls.map(([url]) => url)).toContain("/api/canvas/events?after=1");
});

test("unmount aborts pending replay history", async () => {
  const context = { imageSmoothingEnabled: false, clearRect: vi.fn(), drawImage: vi.fn(), save: vi.fn(), restore: vi.fn(), scale: vi.fn(), createImageData: () => ({ data: new Uint8ClampedArray(64 * 64 * 4) }), putImageData: vi.fn() };
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(context as never);
  let historySignal: AbortSignal | undefined;
  vi.stubGlobal("fetch", vi.fn((_url: string, options?: RequestInit) => {
    historySignal = options?.signal ?? undefined;
    return new Promise(() => {});
  }));
  document.body.innerHTML = '<div id="root"></div>';
  await act(async () => { root = createRoot(document.getElementById("root")!); root.render(<CanvasPage snapshot={snapshot} />); });
  await act(async () => { document.querySelector("button")?.click(); });
  act(() => root?.unmount());
  expect(historySignal?.aborted).toBe(true);
});
