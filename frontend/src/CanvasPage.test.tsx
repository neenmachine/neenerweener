import { expect, test } from "vitest";
import { applyCanvasEvents } from "./CanvasPage";

test("canvas events apply placements and clear regions in order", () => {
  const pixels = new Uint8Array([255, 1, 2, 3, 4, 5, 6, 7, 8]);
  expect([...applyCanvasEvents(pixels, [
    { revision: 1, event_kind: "place", x: 0, y: 0, color: 9 },
    { revision: 2, event_kind: "clear", clear_bounds: { x: 1, y: 0, width: 2, height: 2 } },
  ], 3)]).toEqual([9, 255, 255, 3, 255, 255, 6, 7, 8]);
});
