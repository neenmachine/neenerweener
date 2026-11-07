// @vitest-environment jsdom
// The loaded Admin chunk must not add a second server-rendered robots tag.
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, expect, test, vi } from "vitest";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | undefined;

afterEach(() => {
  act(() => root?.unmount());
  root = undefined;
  document.body.innerHTML = "";
});

test("the loaded admin page keeps one server robots tag", { timeout: 5000 }, async () => {
  document.head.innerHTML = '<meta name="robots" content="noindex">';
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve({ ok: true, status: 200, headers: new Headers(), json: () => Promise.resolve({ authenticated: false }) })));
  document.body.innerHTML = `<script id="preload" type="application/json">{"page":{"kind":"admin"}}</script><div id="root"></div>`;
  await import("./Admin");
  const { default: App } = await import("./App");
  await act(async () => {
    root = createRoot(document.getElementById("root")!);
    root.render(<App />);
  });
  const main = document.querySelector("main")!;
  const shell = main.firstElementChild!;
  expect(shell.matches("article.admin")).toBe(true);
  expect(shell.querySelector("h1")?.textContent).toBe("admin");
  expect(document.head.querySelectorAll('meta[name="robots"]').length).toBe(1);
  const footer = main.querySelector("footer")!;
  expect(shell.compareDocumentPosition(footer) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
});
