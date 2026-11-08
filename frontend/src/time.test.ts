// @vitest-environment jsdom
// The React half of the Toronto time cases; backend/test_app.py runs the same rows against the server formatter.
import { expect, test } from "vitest";
import { compact, full } from "./App";
import cases from "./time_cases.json";
import type { Zone } from "./timelineTypes";

test.each(cases.full)("%s in Toronto is %s: %s", (instant, zone, expected) => {
  expect(full(instant, zone as Zone)).toBe(expected);
});

test.each(cases.compact)("%s at now %s is %s", (instant, now, expected) => {
  expect(compact(instant, now)).toBe(expected);
});
