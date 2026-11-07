// @vitest-environment jsdom
// The server decides not-found; with its preload the app shows the one not-found view and fetches nothing.
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, expect, test, vi } from "vitest";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | undefined;

afterEach(() => {
  act(() => root?.unmount());
  root = undefined;
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
  vi.resetModules();
});

test("an admin page without not-found preload does not render not found", async () => {
  history.replaceState(null, "", "/admin");
  document.body.innerHTML = `<script id="preload" type="application/json">${JSON.stringify({ "/api/posts": [] })}</script><div id="root"></div>`;
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve({ ok: true, status: 200, headers: new Headers(), json: () => Promise.resolve({ authenticated: false }) })));
  const { default: App } = await import("./App");
  await act(async () => {
    root = createRoot(document.getElementById("root")!);
    root.render(<App />);
    await new Promise((resolve) => setTimeout(resolve, 0));
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
  expect(document.querySelector("main")?.textContent ?? "").not.toContain("not found");
  expect(document.querySelector('input[type="password"]')).not.toBeNull();
});

test.each([
  ["/does-not-exist", "not found"],
  ["/posts/nope", "post not found"],
  ["/code/nope", "project not found"],
  ["/code/demo/nope.py", "file not found"],
])("%s renders the server's not-found view without fetching", async (path, label) => {
  const fetch = vi.fn();
  vi.stubGlobal("fetch", fetch);
  history.replaceState(null, "", path);
  document.body.innerHTML = `<script id="preload" type="application/json">${JSON.stringify({ "not-found": label })}</script><div id="root"></div>`;
  const { default: App } = await import("./App");
  await act(async () => {
    root = createRoot(document.getElementById("root")!);
    root.render(<App />);
  });
  const root_ = document.getElementById("root")!;
  expect(root_.querySelectorAll("main")).toHaveLength(1);
  expect([...root_.querySelectorAll("h1")].map((h) => h.textContent)).toEqual([label]);
  expect(root_.querySelector("main > footer")?.innerHTML).toBe('<a href="/">neenerweener</a><a href="/timeline">timeline</a>');
  expect(root_.querySelectorAll('a[href="/"]')).toHaveLength(2);
  expect(fetch).not.toHaveBeenCalled();
});
