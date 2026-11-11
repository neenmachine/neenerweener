import { expect, test } from "vitest";
import { imageFiles, imageMarkdown, insertAt } from "./postPaste";

const file = (name: string, type: string) => new File(["x"], name, { type });
const transfer = (...files: File[]) => ({ files }) as unknown as DataTransfer;

test("only image files are taken, in their order; plain text or no data leaves the browser's own paste", () => {
  const [png, txt, jpeg] = [file("a.png", "image/png"), file("b.txt", "text/plain"), file("c.jpg", "image/jpeg")];
  expect(imageFiles(transfer(png, txt, jpeg))).toEqual([png, jpeg]);
  expect(imageFiles(transfer(txt))).toEqual([]);
  expect(imageFiles(transfer())).toEqual([]);
  expect(imageFiles(null)).toEqual([]);
});

test("the markdown has an empty alt and the large variant", () => {
  expect(imageMarkdown("1234")).toBe("![](/media/1234/lg.webp)");
});

const IMG = "![](/media/1/lg.webp)";

test.each([
  // [case, text at paste, selection, text now, expected text, expected caret]
  ["at the cursor", "ab", [1, 1], "ab", `a${IMG}b`, 1 + IMG.length],
  ["over the selection", "abcd", [1, 3], "abcd", `a${IMG}d`, 1 + IMG.length],
  ["typed after the spot meanwhile", "ab", [1, 1], "abXY", `a${IMG}bXY`, 1 + IMG.length],
  ["typed before the spot meanwhile", "ab", [1, 1], "XYab", `XYa${IMG}b`, 3 + IMG.length],
  ["selection kept when typing came after it", "abcd", [1, 3], "abcdX", `a${IMG}bcdX`, 1 + IMG.length],
  ["both sides changed: at the end", "ab", [1, 1], "XbY", `XbY${IMG}`, 3 + IMG.length],
  ["empty text", "", [0, 0], "", IMG, IMG.length],
] as const)("insertAt %s", (_, text, [start, end], now, expected, caret) => {
  expect(insertAt(now, { text, start, end }, IMG)).toEqual({ text: expected, caret });
});

test("insertAt keeps every other character: nothing outside the inserted markdown changes", () => {
  const text = "x\r\n\ty  $a$\n```\ncode\n```\n";
  for (let at = 0; at <= text.length; at++) {
    const r = insertAt(text, { text, start: at, end: at }, IMG);
    expect(r.text.slice(0, at) + r.text.slice(at + IMG.length)).toBe(text);
  }
});
