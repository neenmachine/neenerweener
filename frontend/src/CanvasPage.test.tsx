// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, expect, test, vi } from "vitest";
import CanvasPage, { applyCanvasEvents, replayDelay, replayPositionToRevision, replayStep, type CanvasSnapshot } from "./CanvasPage";

const snapshot: CanvasSnapshot = { width: 64, height: 64, palette: ["#000000", "#ffffff"], revision: 0, pixels: "" };
let root: Root | undefined;

afterEach(() => {
  act(() => root?.unmount());
  root = undefined;
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

test("replay position seeks revisions, steps within bounds, and scales speed", () => {
  const history = [{ revision: 4, event_kind: "place" as const }, { revision: 9, event_kind: "place" as const }];
  expect(replayPositionToRevision(history, 0)).toBe(0);
  expect(replayPositionToRevision(history, 1)).toBe(4);
  expect(replayPositionToRevision(history, 8)).toBe(9);
  expect(replayStep(0, -1, 2)).toBe(0);
  expect(replayStep(1, 1, 2)).toBe(2);
  expect(replayStep(2, 1, 2)).toBe(2);
  expect(replayDelay(4)).toBe(8.75);
});

test("canvas events apply placements and clear regions in order", () => {
  const pixels = new Uint8Array([255, 1, 2, 3, 4, 5, 6, 7, 8]);
  expect([...applyCanvasEvents(pixels, [
    { revision: 1, event_kind: "place", x: 0, y: 0, color: 9 },
    { revision: 2, event_kind: "clear", clear_bounds: { x: 1, y: 0, width: 2, height: 2 } },
  ], 3)]).toEqual([9, 255, 255, 3, 255, 255, 6, 7, 8]);
});

test("polling pages through a full response without skipping its tail", async () => {
  vi.useFakeTimers();
  const context = { imageSmoothingEnabled: false, clearRect: vi.fn(), drawImage: vi.fn(), save: vi.fn(), restore: vi.fn(), scale: vi.fn(), createImageData: () => ({ data: new Uint8ClampedArray(64 * 64 * 4) }), putImageData: vi.fn() };
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(context as never);
  const firstPage = Array.from({ length: 500 }, (_, index) => ({ revision: index + 1, event_kind: "place" as const, x: 0, y: 0, color: 1 }));
  const fetch = vi.fn((url: string) => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(url.includes("after=0") ? { reset: false, revision: 501, events: firstPage } : { reset: false, revision: 501, events: [{ revision: 501, event_kind: "place", x: 1, y: 1, color: 1 }] }) }));
  vi.stubGlobal("fetch", fetch);
  document.body.innerHTML = '<div id="root"></div>';
  await act(async () => { root = createRoot(document.getElementById("root")!); root.render(<CanvasPage snapshot={snapshot} />); });
  await act(async () => {
    vi.advanceTimersByTime(3000);
    await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
    vi.runOnlyPendingTimers();
    await Promise.resolve(); await Promise.resolve();
  });
  expect(fetch.mock.calls.map(([url]) => url)).toContain("/api/canvas/events?after=500");
});

test("unmount aborts pending replay history", async () => {
  const context = { imageSmoothingEnabled: false, clearRect: vi.fn(), drawImage: vi.fn(), save: vi.fn(), restore: vi.fn(), scale: vi.fn(), createImageData: () => ({ data: new Uint8ClampedArray(64 * 64 * 4) }), putImageData: vi.fn() };
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(context as never);
  let historySignal: AbortSignal | undefined;
  vi.stubGlobal("fetch", vi.fn((_url: string, options?: RequestInit) => {
    historySignal = options?.signal ?? undefined;
    return new Promise((_resolve, reject) => options?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true }));
  }));
  const unhandled: PromiseRejectionEvent[] = [];
  const onUnhandled = (event: PromiseRejectionEvent) => { unhandled.push(event); event.preventDefault(); };
  window.addEventListener("unhandledrejection", onUnhandled);
  document.body.innerHTML = '<div id="root"></div>';
  await act(async () => { root = createRoot(document.getElementById("root")!); root.render(<CanvasPage snapshot={snapshot} />); });
  await act(async () => { document.querySelector("button")?.click(); });
  act(() => root?.unmount());
  await act(async () => { await Promise.resolve(); await Promise.resolve(); });
  window.removeEventListener("unhandledrejection", onUnhandled);
  expect(historySignal?.aborted).toBe(true);
  expect(unhandled).toHaveLength(0);
});
