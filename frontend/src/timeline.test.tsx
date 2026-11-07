// @vitest-environment jsdom
// Drives the real TimelinePage in jsdom: server preload in the page, fetch stubbed per older page.
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

// Counts loads of the composer chunk; the real module still renders. doMock per test, after resetModules,
// so each test loads it afresh (a hoisted vi.mock factory runs once per file).
let composerLoads = 0;
beforeEach(() => {
  composerLoads = 0;
  vi.doMock("./TimelineComposer", async (importOriginal) => {
    composerLoads++;
    return importOriginal();
  });
});

type Page = { entries: { id: number; html: string; created_at: string; edited_at: null }[]; next_cursor: number | null };

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const page = (ids: number[], next_cursor: number | null): Page => ({
  entries: ids.map((id) => ({ id, html: `<p>entry ${id}</p>`, created_at: "2026-01-02T03:04:05+00:00", edited_at: null })),
  next_cursor,
});

let root: Root | undefined;

afterEach(() => {
  document.cookie = "admin_hint=; Max-Age=0; Path=/";
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
  document.body.innerHTML = `<script id="preload" type="application/json">${JSON.stringify({ page: { kind: "timeline" }, "/api/timeline": first })}</script><div id="root"></div>`;
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

// The admin compose box: a hint cookie decides whether to check the session; the session decides the composer.
type Call = [string, RequestInit | undefined];
const reply = (status: number, body: unknown) => ({ ok: status < 300, status, headers: new Headers(), json: () => Promise.resolve(body) });
const created = { id: 10, html: "<p>new</p>", created_at: "2026-01-03T00:00:00+00:00", edited_at: null, body_markdown: "new" };

async function openAdmin(hint: boolean, handle: (url: string, init?: RequestInit) => unknown) {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      calls.push([url, init]);
      return handle(url, init);
    }),
  );
  if (hint) document.cookie = "admin_hint=1; Path=/";
  history.replaceState(null, "", "/timeline");
  document.body.innerHTML = `<script id="preload" type="application/json">${JSON.stringify({ page: { kind: "timeline" }, "/api/timeline": page([4], null) })}</script><div id="root"></div>`;
  const { default: App } = await import("./App");
  await act(async () => {
    root = createRoot(document.getElementById("root")!);
    root.render(<App />);
  });
  return calls;
}
const signedIn = (url: string) => (url === "/api/admin/session" ? reply(200, { authenticated: true, csrf_token: "csrf-secret" }) : reply(201, created));
const textarea = () => document.querySelector<HTMLTextAreaElement>('textarea[aria-label="new timeline entry"]');
const tweet = () => [...document.querySelectorAll("button")].find((b) => b.textContent === "tweet");
const settle = () => act(async () => {
  for (let i = 0; i < 10; i++) await new Promise((r) => setTimeout(r, 0));
});
const type = (value: string) =>
  act(() => {
    const el = textarea()!;
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
  });
const submit = () => act(() => textarea()!.form!.dispatchEvent(new SubmitEvent("submit", { bubbles: true, cancelable: true })));
// Waits in act()-wrapped steps (at most 2 s) for the lazy imports and the session check.
async function until(check: () => boolean, what: string) {
  for (let i = 0; i < 200 && !check(); i++) await act(async () => await new Promise((r) => setTimeout(r, 10)));
  expect(check(), what).toBe(true);
}
const showsComposer = () => until(() => !!tweet(), "tweet button");

test("without the hint there is no session request and no composer chunk", { timeout: 5000 }, async () => {
  const calls = await openAdmin(false, signedIn);
  await settle();
  expect(calls).toEqual([]);
  expect(composerLoads).toBe(0);
  expect(textarea()).toBeNull();
});

test("hint and a signed-in session show the composer, with the CSRF token kept out of the page", { timeout: 5000 }, async () => {
  const calls = await openAdmin(true, signedIn);
  await showsComposer();
  expect(calls.map(([url]) => url)).toEqual(["/api/admin/session"]);
  expect(composerLoads).toBe(1);
  // Right after the heading, before the entries.
  expect(document.querySelector("h1 + form textarea")).toBe(textarea());
  expect(document.documentElement.outerHTML).not.toContain("csrf-secret");
  expect(JSON.stringify({ ...localStorage, ...sessionStorage })).not.toContain("csrf-secret");
});

test("hint and a signed-out session clear the hint and never load the composer", { timeout: 5000 }, async () => {
  const calls = await openAdmin(true, () => reply(200, { authenticated: false }));
  await until(() => !document.cookie.includes("admin_hint"), "hint cleared");
  await settle();
  expect(calls.map(([url]) => url)).toEqual(["/api/admin/session"]);
  expect(composerLoads).toBe(0);
  expect(textarea()).toBeNull();
});

test("a session check that fails keeps the hint for a later visit", { timeout: 5000 }, async () => {
  await openAdmin(true, () => reply(503, { detail: "admin unavailable" }));
  await settle();
  expect(document.cookie).toContain("admin_hint=1");
  expect(textarea()).toBeNull();
});

test("tweet posts with the CSRF token and shows the new entry first, without a reload", { timeout: 5000 }, async () => {
  const calls = await openAdmin(true, signedIn);
  await showsComposer();
  const href = location.href;
  type("new");
  submit();
  await settle();
  const [url, init] = calls[1];
  expect(url).toBe("/api/admin/timeline");
  expect(init?.method).toBe("POST");
  expect(init?.body).toBe('{"body_markdown":"new"}');
  expect(init?.headers).toMatchObject({ "Content-Type": "application/json", "X-CSRF-Token": "csrf-secret" });
  expect(ids()).toEqual(["entry-10", "entry-4"]);
  expect(textarea()!.value).toBe("");
  expect(document.activeElement).toBe(textarea());
  expect(location.href).toBe(href);
});

test("a server error shows inline and keeps the draft; two submits in one task post once", { timeout: 5000 }, async () => {
  const calls = await openAdmin(true, (url) =>
    url === "/api/admin/session" ? signedIn(url) : reply(422, { detail: "invalid timeline entry" }),
  );
  await showsComposer();
  type("draft");
  submit();
  submit();
  await settle();
  expect(calls.filter(([url]) => url === "/api/admin/timeline")).toHaveLength(1);
  expect(document.querySelector("form [role=alert]")?.textContent).toBe("invalid timeline entry");
  expect(textarea()!.value).toBe("draft");
  expect(ids()).toEqual(["entry-4"]);
});

test("tweet stays off only for whitespace-only text", { timeout: 5000 }, async () => {
  const calls = await openAdmin(true, signedIn);
  await showsComposer();
  type("\x85 ");
  expect(tweet()!.getAttribute("aria-disabled")).toBe("true");
  submit();
  await settle();
  expect(calls).toHaveLength(1);
  type("ok");
  expect(tweet()!.getAttribute("aria-disabled")).toBe("false");
});

// The server owns NUL, length and link rules: the client sends the text and shows the 422 inline.
test.each([
  ["NUL", "a\0b", "text must not contain NUL"],
  ["10,001 characters", "x".repeat(10_001), "text is too long"],
])("%s text is sent and the server's 422 shows inline with the draft kept", { timeout: 5000 }, async (_, body, detail) => {
  const calls = await openAdmin(true, (url) => (url === "/api/admin/session" ? signedIn(url) : reply(422, { detail })));
  await showsComposer();
  type(body);
  expect(tweet()!.getAttribute("aria-disabled")).toBe("false");
  submit();
  await settle();
  const posts = calls.filter(([url]) => url === "/api/admin/timeline");
  expect(posts).toHaveLength(1);
  expect(posts[0][1]?.body).toBe(JSON.stringify({ body_markdown: body }));
  expect(document.querySelector("form [role=alert]")?.textContent).toBe(detail);
  expect(textarea()!.value).toBe(body);
});

test("a 401 on tweet removes the composer and the hint", { timeout: 5000 }, async () => {
  await openAdmin(true, (url) => (url === "/api/admin/session" ? signedIn(url) : reply(401, { detail: "authentication required" })));
  await showsComposer();
  type("new");
  submit();
  await settle();
  expect(textarea()).toBeNull();
  expect(document.cookie).not.toContain("admin_hint");
});
