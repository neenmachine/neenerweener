// @vitest-environment jsdom
// While the Admin chunk loads, /admin shows the admin shell above the footer, not the footer alone.
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, expect, test, vi } from "vitest";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// The lazy chunk never loads, so the Suspense fallback stays on screen.
vi.mock("./Admin", () => new Promise(() => {}));

let root: Root | undefined;

afterEach(() => {
  act(() => root?.unmount());
  root = undefined;
  document.body.innerHTML = "";
});

test("the admin fallback is the admin shell above the footer", { timeout: 5000 }, async () => {
  document.body.innerHTML = `<script id="preload" type="application/json">{"page":{"kind":"admin"}}</script><div id="root"></div>`;
  const { default: App } = await import("./App");
  await act(async () => {
    root = createRoot(document.getElementById("root")!);
    root.render(<App />);
  });
  const main = document.querySelector("main")!;
  const shell = main.firstElementChild!;
  expect(shell.matches("article.admin")).toBe(true);
  expect(shell.querySelector("h1")?.textContent).toBe("admin");
  const footer = main.querySelector("footer")!;
  expect(shell.compareDocumentPosition(footer) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
});
