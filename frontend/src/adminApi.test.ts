import { expect, test, vi } from "vitest";
import { checkSession, hasText, request } from "./adminApi";

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
const hex = (c: number) => c.toString(16).toUpperCase().padStart(4, "0");
test.each([...PYTHON_ISSPACE.map((c) => [hex(c), false, c]), ...PYTHON_KEEPS.map((c) => [hex(c), true, c])] as [string, boolean, number][])(
  "U+%s alone: hasText is %s",
  (_, expected, c) => {
    expect(hasText(String.fromCodePoint(c))).toBe(expected);
  },
);

test("a Blob body goes raw with its own type, or application/octet-stream, and the CSRF header", async () => {
  const calls: RequestInit[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit) => {
    calls.push(init);
    return url === "/api/admin/session"
      ? { ok: true, status: 200, headers: new Headers(), json: async () => ({ authenticated: true, csrf_token: "t" }) }
      : { ok: true, status: 201, headers: new Headers(), json: async () => ({ id: "x", width: 1, height: 1 }) };
  }));
  try {
    await checkSession();
    const typed = new File(["png"], "a.png", { type: "image/png" });
    const untyped = new File(["?"], "b");
    await request("POST", "/api/admin/media", typed);
    await request("POST", "/api/admin/media", untyped);
    expect(calls[1]).toMatchObject({ method: "POST", body: typed, headers: { "Content-Type": "image/png", "X-CSRF-Token": "t" } });
    expect(calls[2]).toMatchObject({ body: untyped, headers: { "Content-Type": "application/octet-stream", "X-CSRF-Token": "t" } });
  } finally {
    vi.unstubAllGlobals();
  }
});

// The server's short detail is shown for a too-large body (413) as for a validation error (422).
test.each([
  [413, "request too large", "request too large"],
  [422, "unsupported image", "unsupported image"],
  [422, [{ msg: "schema" }], "invalid text"],
  [413, undefined, "something went wrong"],
])("%s with detail %j shows %s", async (status, detail, message) => {
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status, headers: new Headers(), json: async () => ({ detail }) })));
  try {
    const r = await request("POST", "/api/admin/media", {});
    expect(r).toMatchObject({ ok: false, status, message });
  } finally {
    vi.unstubAllGlobals();
  }
});
