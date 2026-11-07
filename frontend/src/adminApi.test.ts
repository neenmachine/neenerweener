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

// Never stricter than the server: a lone character is "no text" exactly when Python's str.strip() removes it.
// From Python: [c for c in map(chr, range(0x110000)) if c.isspace()] (29 code points).
const PYTHON_ISSPACE = [
  0x0009, 0x000a, 0x000b, 0x000c, 0x000d, 0x001c, 0x001d, 0x001e, 0x001f, 0x0020, 0x0085, 0x00a0, 0x1680, 0x2000, 0x2001,
  0x2002, 0x2003, 0x2004, 0x2005, 0x2006, 0x2007, 0x2008, 0x2009, 0x200a, 0x2028, 0x2029, 0x202f, 0x205f, 0x3000,
];
// Near-misses Python keeps: zero-width space, BOM, Mongolian vowel separator, word joiner.
const PYTHON_KEEPS = [0x200b, 0xfeff, 0x180e, 0x2060];
test("python whitespace table has 29 code points", () => expect(PYTHON_ISSPACE).toHaveLength(29));
test.each([...PYTHON_ISSPACE.map((c) => [c, false]), ...PYTHON_KEEPS.map((c) => [c, true])] as [number, boolean][])(
  "U+%s alone: hasText is %s",
  (c, expected) => {
    expect(hasText(String.fromCodePoint(c))).toBe(expected);
  },
);
