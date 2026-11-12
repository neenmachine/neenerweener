// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, expect, test, vi } from "vitest";
import CanvasPage, { applyCanvasEvents, replayDelay, replayPositionToRevision, replayStep, type CanvasSnapshot } from "./CanvasPage";
import { useCanvasMode, useCanvasReplay } from "./canvasReplay";

const blank = new Uint8Array(64 * 64).fill(255);
const encoded = (pixels = blank) => btoa(String.fromCharCode(...pixels));
const snapshot: CanvasSnapshot = { width: 64, height: 64, palette: ["#000000", "#ffffff"], revision: 0, pixels: "" };
const liveSnapshot: CanvasSnapshot = { ...snapshot, revision: 1, pixels: encoded(Uint8Array.from(blank, (_, index) => index === 0 ? 0 : 255)) };

function mockCanvas() {
  const context = { imageSmoothingEnabled: false, clearRect: vi.fn(), drawImage: vi.fn(), createImageData: () => ({ data: new Uint8ClampedArray(64 * 64 * 4) }), putImageData: vi.fn() };
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(context as never);
  return context;
}
async function flush() { await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); }
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
  await act(async () => { root = createRoot(document.getElementById("root")!); root.render(<CanvasPage snapshot={liveSnapshot} />); });
  await act(async () => { document.querySelector("button")?.click(); });
  act(() => root?.unmount());
  await act(async () => { await Promise.resolve(); await Promise.resolve(); });
  window.removeEventListener("unhandledrejection", onUnhandled);
  expect(historySignal?.aborted).toBe(true);
  expect(unhandled).toHaveLength(0);
});

test("a reset event reloads the live snapshot", async () => {
  vi.useFakeTimers();
  mockCanvas();
  const restored = { ...liveSnapshot, revision: 9 };
  const fetch = vi.fn((url: string) => Promise.resolve({ ok: true, json: () => Promise.resolve(url === "/api/canvas" ? restored : { reset: true, revision: 9, events: [] }) }));
  vi.stubGlobal("fetch", fetch);
  document.body.innerHTML = '<div id="root"></div>';
  await act(async () => { root = createRoot(document.getElementById("root")!); root.render(<CanvasPage snapshot={liveSnapshot} />); });
  await act(async () => { vi.advanceTimersByTime(3000); await flush(); vi.runOnlyPendingTimers(); await flush(); });
  expect(fetch.mock.calls.map(([url]) => url)).toContain("/api/canvas");
  expect(document.querySelector(".canvas-live")?.textContent).toContain("rev 9");
});

test("a poll response that arrives after replay starts cannot overwrite replay", async () => {
  vi.useFakeTimers();
  mockCanvas();
  let resolvePoll!: (value: unknown) => void;
  const fetch = vi.fn((url: string) => {
    if (url.startsWith("/api/canvas/events")) return new Promise((resolve) => { resolvePoll = resolve; });
    return Promise.resolve({ ok: true, json: () => Promise.resolve({ reset: false, revision: 1, events: [] }) });
  });
  vi.stubGlobal("fetch", fetch);
  document.body.innerHTML = '<div id="root"></div>';
  await act(async () => { root = createRoot(document.getElementById("root")!); root.render(<CanvasPage snapshot={liveSnapshot} />); });
  await act(async () => { vi.advanceTimersByTime(3000); await flush(); });
  await act(async () => { (document.querySelector("button.canvas-pill-action") as HTMLButtonElement).click(); await flush(); });
  await act(async () => { resolvePoll({ ok: true, json: () => Promise.resolve({ reset: false, revision: 2, events: [{ revision: 2, event_kind: "place", x: 1, y: 1, color: 0, handle: "stale" }] }) }); await flush(); });
  expect(document.querySelector(".canvas-activity")?.textContent).not.toContain("stale");
  expect(document.querySelector(".canvas-position")?.textContent).toContain("rev 0");
});

test("stopping replay commits the restored live revision and empty state", async () => {
  mockCanvas();
  const restored = { ...liveSnapshot, revision: 7, pixels: encoded() };
  const fetch = vi.fn((url: string) => Promise.resolve({ ok: true, json: () => Promise.resolve(url === "/api/canvas" ? restored : url.startsWith("/api/canvas/history") ? { reset: false, revision: 1, events: [{ revision: 1, event_kind: "place", x: 0, y: 0, color: 0 }] } : { reset: false, revision: 7, events: [] }) }));
  vi.stubGlobal("fetch", fetch);
  document.body.innerHTML = '<div id="root"></div>';
  await act(async () => { root = createRoot(document.getElementById("root")!); root.render(<CanvasPage snapshot={liveSnapshot} />); });
  await act(async () => { (document.querySelector("button.canvas-pill-action") as HTMLButtonElement).click(); await flush(); });
  await act(async () => { (document.querySelector(".canvas-stop") as HTMLButtonElement).click(); await flush(); await flush(); });
  expect(fetch.mock.calls.map(([url]) => url)).toContain("/api/canvas");
  await act(async () => { await vi.waitFor(() => expect(document.querySelector(".canvas-live")?.textContent).toContain("rev 7")); });
  expect(document.querySelector(".canvas-empty-label")?.textContent).toBe("no pixels yet");
  expect(document.querySelector("button.canvas-pill-action")?.getAttribute("aria-disabled")).toBe("true");
});

function ReplayHarness({ restore }: { restore: (controller: AbortController) => Promise<boolean> }) {
  const canvas = { current: document.createElement("canvas") };
  const mode = useCanvasMode();
  const replay = useCanvasReplay({ mode, live: { snapshot: liveSnapshot, restore, reportStatus: vi.fn() }, canvas, reduced: true });
  return <div data-active={replay.active} data-playing={replay.playing} data-revision={replay.revision} data-position={replay.position}>
    <button onClick={() => void replay.start()}>start</button><button onClick={replay.togglePlaying}>play</button><button onClick={() => replay.seek(1)}>seek</button><button onClick={replay.beginSeek}>drag</button><button onClick={replay.endSeek}>release</button><button onClick={() => void replay.stop()}>stop</button>
  </div>;
}

test("the replay module seeks, stops at the end, resumes after drag, and restores live", async () => {
  vi.useFakeTimers();
  mockCanvas();
  const restore = vi.fn(() => Promise.resolve(true));
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve({ ok: true, json: () => Promise.resolve({ reset: false, revision: 2, events: [{ revision: 1, event_kind: "place", x: 0, y: 0, color: 0 }, { revision: 2, event_kind: "place", x: 1, y: 0, color: 1 }] }) })));
  document.body.innerHTML = '<div id="root"></div>';
  await act(async () => { root = createRoot(document.getElementById("root")!); root.render(<ReplayHarness restore={restore} />); });
  const buttons = [...document.querySelectorAll("button")];
  await act(async () => { buttons[0].click(); await flush(); });
  await act(async () => { buttons[2].click(); });
  expect(document.querySelector("div[data-revision]")?.getAttribute("data-revision")).toBe("1");
  await act(async () => { buttons[1].click(); });
  await act(async () => { buttons[3].click(); });
  await act(async () => { buttons[4].click(); });
  expect(document.querySelector("div[data-playing]")?.getAttribute("data-playing")).toBe("true");
  await act(async () => { vi.advanceTimersByTime(40); await flush(); });
  await act(async () => { vi.advanceTimersByTime(40); await flush(); });
  expect(document.querySelector("div[data-playing]")?.getAttribute("data-playing")).toBe("false");
  await act(async () => { buttons[5].click(); await flush(); });
  expect(restore).toHaveBeenCalledOnce();
  expect(document.querySelector("div[data-active]")?.getAttribute("data-active")).toBe("false");
});
