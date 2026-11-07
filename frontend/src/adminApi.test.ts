import { expect, test } from "vitest";
import { hasText } from "./adminApi";

// Only whitespace-only text is held back, and only characters Python's str.strip() also removes; the server decides the rest.
test.each([
  ["empty", "", false],
  ["spaces and newlines", " \n\t\r", false],
  ["python-only whitespace U+001C and U+0085", "\x1c\x85", false],
  ["unicode spaces", "\u00a0\u2000\u3000\u2028", false],
  ["BOM, which JS trim() strips but Python keeps", "\ufeff", true],
  ["one letter", "a", true],
  ["NUL, which the server refuses", "a\0b", true],
  ["10,001 letters, which the server refuses", "x".repeat(10_001), true],
])("%s", (_, body, expected) => {
  expect(hasText(body)).toBe(expected);
});
