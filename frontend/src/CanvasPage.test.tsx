import { expect, test } from "vitest";
import { applyCanvasEvents, loadCanvasHistory } from "./CanvasPage";

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
  const history = await loadCanvasHistory(async (after) => {
    calls.push(after);
    const page = after === 0 ? pages[0] : pages[1];
    return { reset: false, revision: 501, events: page };
  }, new AbortController().signal);
  expect(history).toHaveLength(501);
  expect(calls).toEqual([0, 500]);
});

test("history starts from the empty canvas rather than the live snapshot", () => {
  const live = new Uint8Array([7, 255, 255, 255]);
  const empty = new Uint8Array(live.length).fill(255);
  expect([...applyCanvasEvents(empty, [{ revision: 1, event_kind: "place", x: 0, y: 0, color: 2 }], 2)]).toEqual([2, 255, 255, 255]);
  expect(live[0]).toBe(7);
});
