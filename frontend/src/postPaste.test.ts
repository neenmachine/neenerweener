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

const B = `\n\n${IMG}\n\n`; // in the middle of a line: a block of its own

test.each([
  // [case, text at paste, selection, text now, expected text, expected caret]
  ["at the cursor", "ab", [1, 1], "ab", `a${B}b`, 1 + B.length],
  ["over the selection", "abcd", [1, 3], "abcd", `a${B}d`, 1 + B.length],
  ["typed after the spot meanwhile", "ab", [1, 1], "abXY", `a${B}bXY`, 1 + B.length],
  ["typed before the spot meanwhile", "ab", [1, 1], "XYab", `XYa${B}b`, 3 + B.length],
  ["selection kept when typing came after it", "abcd", [1, 3], "abcdX", `a${B}bcdX`, 1 + B.length],
  ["both sides changed: at the end", "ab", [1, 1], "XbY", `XbY\n\n${IMG}`, 5 + IMG.length],
  ["empty text", "", [0, 0], "", IMG, IMG.length],
  ["start of the text", "ab", [0, 0], "ab", `${IMG}\n\nab`, IMG.length + 2],
  ["empty line between paragraphs", "a\n\n\nb", [2, 2], "a\n\n\nb", `a\n\n${IMG}\n\nb`, 3 + IMG.length],
  ["end of a line", "a\nb", [1, 1], "a\nb", `a\n\n${IMG}\n\nb`, 4 + IMG.length],
] as const)("insertAt %s", (_, text, [start, end], now, expected, caret) => {
  expect(insertAt(now, { text, start, end }, IMG)).toEqual({ text: expected, caret });
});

test("insertAt keeps every other character: only the markdown and blank lines around it go in", () => {
  const text = "x\r\n\ty  $a$\n```\ncode\n```\n";
  for (let at = 0; at <= text.length; at++) {
    const r = insertAt(text, { text, start: at, end: at }, IMG);
    const [head, tail] = r.text.split(IMG);
    expect(head.replace(/\n*$/, "") + tail.replace(/^\n*/, "")).toBe(text.slice(0, at).replace(/\n*$/, "") + text.slice(at).replace(/^\n*/, ""));
    expect(head.startsWith(text.slice(0, at)) && tail.endsWith(text.slice(at))).toBe(true);
  }
});
