import { expect, test } from "vitest";
import { changedRange } from "./textEdit";

test.each([
  // [case, before, after, expected range]
  ["same text", "abc", "abc", { start: 3, end: 3, insert: "" }],
  ["insert in the middle", "ab", "aXb", { start: 1, end: 1, insert: "X" }],
  ["delete", "aXYb", "ab", { start: 1, end: 3, insert: "" }],
  ["replace", "a 640 b", "a 320 b", { start: 2, end: 4, insert: "32" }],
  ["move a block", "P\n\nI\n\nQ", "P\n\nQ\n\nI", { start: 3, end: 7, insert: "Q\n\nI" }],
  ["from empty", "", "ab", { start: 0, end: 0, insert: "ab" }],
  ["repeated letters", "aaa", "aaaa", { start: 3, end: 3, insert: "a" }],
  ["never half an emoji", "x\u{1F600}y", "x\u{1F601}y", { start: 1, end: 3, insert: "\u{1F601}" }],
] as const)("changedRange %s", (_, before, after, range) => {
  const r = changedRange(before, after);
  expect(r).toEqual(range);
  expect(before.slice(0, r.start) + r.insert + before.slice(r.end)).toBe(after);
});
