// @vitest-environment jsdom
// Drives the real TimelinePage in jsdom: server preload in the page, fetch stubbed per older page.
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import type { Entry } from "./timelineTypes";

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

type Page = { entries: Entry[]; next_cursor: number | null };

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const page = (ids: number[], next_cursor: number | null): Page => ({
  entries: ids.map((id) => ({ id, html: `<p>entry ${id}</p>`, created_at: "2026-01-02T03:04:05+00:00", edited_at: null, media: [] })),
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
const photo = (n: number, alt: string) => ({ id: `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`, width: 2048, height: 1536, alt });
// The create API returns the entry with its images, as /api/timeline does.
const created = {
  id: 10,
  html: "<p>new</p>",
  created_at: "2026-01-03T00:00:00+00:00",
  edited_at: null,
  media: [photo(1, '<script>alert("x")</script>'), photo(2, ""), photo(3, "c")],
  body_markdown: "new",
};

async function openAdmin(hint: boolean, handle: (url: string, init?: RequestInit) => unknown, first = page([4], null)) {
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
  document.body.innerHTML = `<script id="preload" type="application/json">${JSON.stringify({ page: { kind: "timeline" }, "/api/timeline": first })}</script><div id="root"></div>`;
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
  // The new entry's images use the same markup as the server's: a three-image grid, alt kept as text.
  const media = document.querySelector("#entry-10 > .media");
  expect(media?.className).toBe("media media-3");
  expect([...media!.querySelectorAll("img")].map((img) => img.getAttribute("alt"))).toEqual(['<script>alert("x")</script>', "", "c"]);
  expect(media!.querySelector("script")).toBeNull();
  expect(media!.querySelector("a")?.getAttribute("href")).toBe("/media/00000000-0000-4000-8000-000000000001/lg.webp");
  expect(document.querySelector("#entry-4 > .media")).toBeNull();
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
  expect(document.querySelector("#compose-error")?.textContent).toBe("invalid timeline entry");
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
  expect(document.querySelector("#compose-error")?.textContent).toBe(detail);
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

test("load more after a tweet uses the preload cursor, appends the older page and shows each entry once", { timeout: 5000 }, async () => {
  // The older page repeats a preload entry (8) and the new entry (10).
  const calls = await openAdmin(
    true,
    (url) => (url === "/api/timeline?before=8" ? reply(200, page([8, 10, 7], null)) : signedIn(url)),
    page([9, 8], 8),
  );
  await showsComposer();
  type("new");
  submit();
  await settle();
  expect(ids()).toEqual(["entry-10", "entry-9", "entry-8"]);
  expect(button()?.getAttribute("href")).toBe("/timeline?before=8");
  click();
  await settle();
  expect(calls.map(([url]) => url)).toEqual(["/api/admin/session", "/api/admin/timeline", "/api/timeline?before=8"]);
  expect(ids()).toEqual(["entry-10", "entry-9", "entry-8", "entry-7"]);
  // The composer has its own status region ("posted"); the page's comes last.
  expect([...document.querySelectorAll('[role="status"]')].at(-1)?.textContent).toBe("1 more loaded, end of timeline");
});

// Attaching images in the composer: the shared upload module, generated in-memory files only.
const png = (name: string) => new File([new Uint8Array([0x89, 0x50, 0x4e, 0x47, name.length])], name, { type: "image/png" });
function previews() {
  let n = 0;
  const revoked: string[] = [];
  URL.createObjectURL = vi.fn(() => `blob:preview-${n++}`);
  URL.revokeObjectURL = vi.fn((url: string) => void revoked.push(url));
  return revoked;
}
const pick = (files: File[]) =>
  act(() => {
    const field = document.querySelector<HTMLInputElement>("#compose-files")!;
    Object.defineProperty(field, "files", { value: files, configurable: true });
    field.dispatchEvent(new Event("change", { bubbles: true }));
  });
const rows = () => document.querySelectorAll("form .attachment");
const alt = (n: number, value: string) =>
  act(() => {
    const el = document.querySelectorAll<HTMLInputElement>("form .attachment input")[n];
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
  });
// Uploads answer id-0, id-1, ... unless upload() says otherwise.
const withMedia =
  (create: () => unknown = () => reply(201, created), upload: (n: number) => unknown = (n) => reply(201, { id: `id-${n}`, width: 4, height: 3 })) => {
    let n = 0;
    return (url: string) => (url === "/api/admin/media" ? upload(n++) : url === "/api/admin/timeline" ? create() : signedIn(url));
  };

test("attached images upload with the CSRF token, go in the create payload in order with alt, and the entry shows them first", { timeout: 5000 }, async () => {
  const revoked = previews();
  const calls = await openAdmin(true, withMedia());
  await showsComposer();
  expect(document.querySelector('label[for="compose-files"]')?.textContent).toBe("attach images");
  const files = [png("a.png"), png("bb.png")];
  pick(files);
  await settle();
  const uploads = calls.filter(([url]) => url === "/api/admin/media");
  expect(uploads.map(([, init]) => init?.body)).toEqual(files);
  for (const [, init] of uploads) expect(init?.headers).toMatchObject({ "X-CSRF-Token": "csrf-secret", "Content-Type": "image/png" });
  expect(rows()).toHaveLength(2);
  alt(1, "second");
  type("new");
  submit();
  await settle();
  const post = calls.find(([url]) => url === "/api/admin/timeline");
  expect(JSON.parse(post?.[1]?.body as string)).toEqual({
    body_markdown: "new",
    attachments: [
      { id: "id-0", alt: "" },
      { id: "id-1", alt: "second" },
    ],
  });
  expect(ids()).toEqual(["entry-10", "entry-4"]);
  expect(document.querySelector("#entry-10 > .media")?.className).toBe("media media-3");
  // The form is empty again and the previews are freed.
  expect(rows()).toHaveLength(0);
  expect(revoked).toEqual(["blob:preview-0", "blob:preview-1"]);
});

test("tweet is off while an image uploads and on once it is done", { timeout: 5000 }, async () => {
  previews();
  let release!: () => void;
  const calls = await openAdmin(
    true,
    withMedia(undefined, () => new Promise((resolve) => (release = () => resolve(reply(201, { id: "id-0", width: 4, height: 3 }))))),
  );
  await showsComposer();
  type("new");
  expect(document.querySelector('label[for="compose-files"]')?.textContent).toBe("attach images");
  pick([png("slow")]);
  await settle();
  expect(document.querySelector("#compose-files-status")?.textContent).toBe("uploading");
  expect(tweet()!.getAttribute("aria-disabled")).toBe("true");
  submit();
  await settle();
  expect(calls.filter(([url]) => url === "/api/admin/timeline")).toHaveLength(0);
  release();
  await settle();
  expect(tweet()!.getAttribute("aria-disabled")).toBe("false");
});

test("a create failure shows the server's detail and keeps the text, the images and their alt", { timeout: 5000 }, async () => {
  const revoked = previews();
  await openAdmin(true, withMedia(() => reply(422, { detail: "unknown image" })));
  await showsComposer();
  expect(document.querySelector('label[for="compose-files"]')?.textContent).toBe("attach images");
  pick([png("a")]);
  await settle();
  alt(0, "kept alt");
  type("draft");
  submit();
  await settle();
  expect(document.querySelector("#compose-error")?.textContent).toBe("unknown image");
  expect(textarea()!.value).toBe("draft");
  expect(rows()).toHaveLength(1);
  expect(document.querySelector<HTMLInputElement>("form .attachment input")!.value).toBe("kept alt");
  expect(revoked).toEqual([]);
  expect(ids()).toEqual(["entry-4"]);
});

test("an upload failure shows the server's detail; a 401 on upload removes the composer and the hint", { timeout: 5000 }, async () => {
  previews();
  let status = 413;
  await openAdmin(true, withMedia(undefined, () => reply(status, { detail: "image too large" })));
  await showsComposer();
  expect(document.querySelector('label[for="compose-files"]')?.textContent).toBe("attach images");
  pick([png("big")]);
  await settle();
  expect(document.querySelector("#compose-files-error")?.textContent).toBe("image too large");
  expect(rows()).toHaveLength(0);
  status = 401;
  pick([png("a")]);
  await settle();
  expect(textarea()).toBeNull();
  expect(document.cookie).not.toContain("admin_hint");
});
