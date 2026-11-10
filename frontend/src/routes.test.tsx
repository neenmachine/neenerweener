// @vitest-environment jsdom
// The React half of the route table: given the server's preload for each URL, the page renders what the server
// decided, with no route list of its own. backend/test_routes.py runs the same rows against the server.
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, expect, test, vi } from "vitest";
import cases from "./route_cases.json";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

type Row = { path: string; page?: object; data?: object; fetched?: Record<string, unknown>; h1?: string[]; markup?: string };

let root: Root | undefined;

afterEach(() => {
  act(() => root?.unmount());
  root = undefined;
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
  vi.resetModules();
});

async function render(path: string, preload: object, fetched: Record<string, unknown> = {}, title = "server title") {
  const fetch = vi.fn((url: string) =>
    Promise.resolve({ ok: true, status: 200, headers: new Headers(), json: () => Promise.resolve(fetched[url]) }),
  );
  vi.stubGlobal("fetch", fetch);
  history.replaceState(null, "", path);
  // Escaped like the server's preload, so data holding "</script>" stays inside the script.
  const json = JSON.stringify(preload).replace(/</g, "\\u003c");
  document.body.innerHTML = `<script id="preload" type="application/json">${json}</script><div id="root"></div>`;
  document.title = title;
  await import("./Admin"); // the lazy chunk, loaded up front so the admin page renders within the wait
  const { default: App } = await import("./App");
  await act(async () => {
    root = createRoot(document.getElementById("root")!);
    root.render(<App />);
    await new Promise((resolve) => setTimeout(resolve, 20));
  });
  return fetch;
}

const pages = (cases.rows as Row[]).filter((row) => row.page);

test.each(pages.map((row) => [row.path, row] as const))("%s renders the server's page", async (_, row) => {
  const fetch = await render(row.path, { page: row.page, ...row.data }, row.fetched);
  const headings = [...document.querySelectorAll('h1, [role="heading"][aria-level="1"]')];
  expect(headings.map((h) => h.textContent?.trim())).toEqual(row.h1);
  expect(fetch.mock.calls.map(([url]) => url)).toEqual(Object.keys(row.fetched ?? {}));
  if (row.markup) expect(document.getElementById("root")!.innerHTML).toBe(row.markup);
});

test.each(pages.map((row) => [row.path, row] as const))("%s has one footer rss link", async (_, row) => {
  await render(row.path, { page: row.page, ...row.data }, row.fetched);
  expect(document.querySelectorAll("footer")).toHaveLength(1);
  const footer = document.querySelector("main > footer")!;
  expect(footer.querySelectorAll('a[href="/feed.xml"]')).toHaveLength(1);
  expect(footer.querySelector('a[href="/feed.xml"]')!.outerHTML).toBe('<a href="/feed.xml">rss</a>');
  const current = [...footer.querySelectorAll('a[aria-current="page"]')].map((a) => a.getAttribute("href"));
  expect(current).toEqual(({ "/": ["/"], "/timeline": ["/timeline"] } as Record<string, string[]>)[row.path] ?? []);
});

test("rendering a code page keeps the server document title", async () => {
  await render("/code/demo/hello.py", { page: { kind: "code", project: "demo", path: "hello.py" } }, { "/api/code/demo": { name: "demo", files: ["hello.py"], readme_html: null }, "/api/code/demo/file/hello.py": { path: "hello.py", html: "<span>print(1)</span>" } }, "hello.py · demo");
  expect(document.title).toBe("hello.py · demo");
});

test("a post title links nowhere; the back arrow sits beside it", async () => {
  await render("/posts/hello", { page: { kind: "post", slug: "hello" }, "/api/posts/hello": { slug: "hello", title: "hello", excerpt: null, tags: [], draft: false, html: "<p>body</p>" } });
  expect(document.querySelector("h1 a")).toBeNull();
  expect(document.querySelector('a[aria-label="back"]')).not.toBeNull();
});

test("a project README's h1 is demoted below the page h1", async () => {
  await render("/code/demo", { page: { kind: "code", project: "demo" } }, { "/api/code/demo": { name: "demo", files: ["hello.py"], readme_html: '<h2 class="readme-h1">demo</h2>' } });
  expect(document.querySelectorAll("h1")).toHaveLength(1);
  expect(document.querySelector(".readme-body h1")).toBeNull();
});

test("the admin page shows its login form", async () => {
  await render("/admin", { page: { kind: "admin" } }, { "/api/admin/session": { authenticated: false } });
  expect(document.querySelector('input[type="password"]')).not.toBeNull();
});

test("a page kind this bundle does not know shows not found", async () => {
  await render("/new", { page: { kind: "from-a-newer-server" } });
  expect([...document.querySelectorAll("h1")].map((h) => h.textContent)).toEqual(["not found"]);
});
