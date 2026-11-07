// @vitest-environment jsdom
// Drives the real TimelinePage in jsdom: server preload in the page, fetch stubbed per older page.
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, expect, test, vi } from "vitest";

type Page = { entries: { id: number; html: string; created_at: string; edited_at: null }[]; next_cursor: number | null };

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const page = (ids: number[], next_cursor: number | null): Page => ({
  entries: ids.map((id) => ({ id, html: `<p>entry ${id}</p>`, created_at: "2026-01-02T03:04:05+00:00", edited_at: null })),
  next_cursor,
});

let root: Root | undefined;

afterEach(() => {
  act(() => root?.unmount());
  root = undefined;
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
  vi.resetModules();
});

// Older pages answer in order; each answer waits until release() so a test can act while it is in flight.
async function open(first: Page, older: Page[], query = "") {
  const releases: (() => void)[] = [];
  const fetch = vi.fn(
    () =>
      new Promise((resolve) => {
        const next = older.shift();
        releases.push(() => resolve({ ok: true, json: () => Promise.resolve(next) }));
      }),
  );
  vi.stubGlobal("fetch", fetch);
  history.replaceState(null, "", `/timeline${query}`);
  document.body.innerHTML = `<script id="preload" type="application/json">${JSON.stringify({ "/api/timeline": first })}</script><div id="root"></div>`;
  const { default: App } = await import("./App");
  act(() => {
    root = createRoot(document.getElementById("root")!);
    root.render(<App />);
  });
  const release = async () => {
    await act(async () => {
      releases.shift()?.();
      await new Promise((r) => setTimeout(r, 0));
    });
  };
  return { fetch, release };
}

const text = () => document.querySelector("main > article")?.textContent ?? "";
const status = () => document.querySelector('[role="status"]')?.textContent;
const button = () => document.querySelector<HTMLAnchorElement>(".load-more a");
const ids = () => [...document.querySelectorAll(".timeline-entry")].map((e) => e.id);
const click = () => {
  expect(button(), "load more button").not.toBeNull();
  act(() => button()!.click());
};

test("the client uses the canonical preload despite duplicate URL cursors", async () => {
  const { fetch } = await open(page([4], 11), [], "?before=22&before=11");
  expect(ids()).toEqual(["entry-4"]);
  expect(button()?.getAttribute("href")).toBe("/timeline?before=11");
  expect(fetch).not.toHaveBeenCalled();
});

test("an empty page with a cursor keeps load more and shows the older entries", async () => {
  const { fetch, release } = await open(page([], 11), [page([5], null)]);
  expect(text()).not.toContain("nothing here yet");
  expect(button()?.textContent ?? "").toContain("load more");
  expect(button()?.getAttribute("href")).toBe("/timeline?before=11");
  click();
  expect(status()).toBe("loading");
  await release();
  expect(fetch).toHaveBeenCalledWith("/api/timeline?before=11");
  expect(ids()).toEqual(["entry-5"]);
  expect(status()).toBe("1 more loaded, end of timeline");
});

test("rapid duplicate activation fetches one page", async () => {
  const { fetch, release } = await open(page([9], 9), [page([8], 8), page([7], null)]);
  click();
  click();
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(button()?.getAttribute("aria-busy")).toBe("true");
  await release();
  expect(ids()).toEqual(["entry-9", "entry-8"]);
  expect(status()).toBe("1 more loaded");
});

test("entries repeated across pages render once", async () => {
  const { release } = await open(page([9, 8], 8), [page([8, 7], null)]);
  click();
  await release();
  expect(ids()).toEqual(["entry-9", "entry-8", "entry-7"]);
  expect(status()).toBe("1 more loaded, end of timeline");
});

test("the last page moves focus from the removed button to the first new entry", async () => {
  const { release } = await open(page([9], 9), [page([8, 7], null)]);
  button()?.focus();
  click();
  await release();
  expect(button()).toBeNull();
  expect(document.activeElement?.closest(".timeline-entry")?.id).toBe("entry-8");
  expect(status()).toBe("2 more loaded, end of timeline");
});

test("an empty last page after an empty page ends on the empty state, with focus on it", async () => {
  const { release } = await open(page([], 11), [page([], null)]);
  button()?.focus();
  click();
  await release();
  expect(button()).toBeNull();
  expect(text()).toContain("nothing here yet");
  expect(document.activeElement?.textContent).toBe("nothing here yet");
  expect(status()).toBe("0 more loaded, end of timeline");
});

test("index.html keeps the post list route", async () => {
  history.replaceState(null, "", "/index.html");
  document.body.innerHTML = `<script id="preload" type="application/json">${JSON.stringify({ "/api/posts": [{ slug: "post", title: "post", excerpt: null, tags: [], draft: false }] })}</script><div id="root"></div>`;
  const { default: App } = await import("./App");
  act(() => {
    root = createRoot(document.getElementById("root")!);
    root.render(<App />);
  });
  expect(document.querySelector("main")?.textContent).toContain("post");
});
