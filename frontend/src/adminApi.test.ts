import { expect, test } from "vitest";
import { validTimelineBody } from "./adminApi";

// The server's rule (backend/timeline.py): no NUL, not only str.strip() whitespace, at most 10,000 code points.
test.each([
  ["empty", "", false],
  ["spaces and newlines", " \n\t\r", false],
  ["python-only whitespace U+001C and U+0085", "\x1c\x85", false],
  ["unicode spaces", "\u00a0\u2000\u3000\u2028", false],
  ["NUL", "a\0b", false],
  ["BOM, which JS trim() strips but Python keeps", "\ufeff", true],
  ["one letter", "a", true],
  ["10,000 letters", "x".repeat(10_000), true],
  ["10,001 letters", "x".repeat(10_001), false],
  ["10,000 astral code points (20,000 UTF-16 units)", "😀".repeat(10_000), true],
  ["10,001 astral code points", "😀".repeat(10_001), false],
])("%s", (_, body, valid) => {
  expect(validTimelineBody(body)).toBe(valid);
});
