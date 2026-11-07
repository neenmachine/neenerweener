// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, expect, test, vi } from "vitest";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

type Media = { id: string; width: number; height: number; alt: string };
type Entry = { id: number; html: string; created_at: string; edited_at: string | null; media: Media[] };
const entry = (id: number, body = `entry ${id}`, media: Media[] = []): Entry => ({ id, html: `<p>${body}</p>`, created_at: "2026-01-02T03:04:05Z", edited_at: null, media });
const ok = (body: unknown, headers = new Headers()) => ({ ok: true, status: 200, headers, json: () => Promise.resolve(body) });
const bad = (status: number, detail: unknown, headers = new Headers()) => ({ ok: false, status, headers, json: () => Promise.resolve({ detail }) });
const session = { authenticated: true, csrf_token: "csrf" };
let root: Root | undefined;

afterEach(() => {
  act(() => root?.unmount());
  root = undefined;
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
  vi.resetModules();
});

async function render(fetch: (url: string, init?: RequestInit) => Promise<unknown>, entries: Entry[] = [entry(1)]) {
  vi.stubGlobal("fetch", vi.fn(fetch));
  document.body.innerHTML = '<div id="root"></div>';
  const { default: Admin } = await import("./Admin");
  await act(async () => {
    root = createRoot(document.getElementById("root")!);
    root.render(<Admin />);
    await new Promise((resolve) => setTimeout(resolve, 0));
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
  return { entries };
}
const input = (selector: string, value: string) => {
  const el = document.querySelector<HTMLInputElement | HTMLTextAreaElement>(selector)!;
  act(() => {
    const setter = Object.getOwnPropertyDescriptor(el.constructor.prototype, "value")!.set!;
    setter.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
  });
  return el;
};
const click = (label: string) => act(() => {
  const button = [...document.querySelectorAll("button")].find((b) => b.textContent?.startsWith(label) && b.getAttribute("aria-busy") !== "true");
  if (!button) throw new Error(`missing ${label}: ${document.body.textContent}`);
  button.click();
});
const submit = (form: HTMLFormElement) => act(() => form.dispatchEvent(new SubmitEvent("submit", { bubbles: true, cancelable: true })));
const flush = () => act(async () => await new Promise((resolve) => setTimeout(resolve, 0)));
const json = (id: number, body = "markdown") => ({ ...entry(id, body), body_markdown: body });

function loggedInFetch(pages: Entry[][] = [[entry(1)]]) {
  let page = 0;
  return vi.fn(async (url: string, init?: RequestInit) => {
    if (url === "/api/admin/session") return ok(session);
    if (url === "/api/timeline" || url.startsWith("/api/timeline?")) return ok({ entries: pages[page++] ?? [], next_cursor: page < pages.length ? 2 : null });
    return ok(json(1));
  });
}

test("login uses an uncontrolled password, clears it, and sends the expected request", async () => {
  const calls: [string, RequestInit | undefined][] = [];
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    calls.push([url, init]);
    if (url === "/api/admin/session") return ok({ authenticated: false });
    if (url === "/api/admin/login") return bad(401, "no");
    return ok({});
  });
  await render(fetch);
  const password = input("#password", "secret");
  submit(password.form!);
  await flush();
  expect(calls[1]).toEqual(["/api/admin/login", expect.objectContaining({ method: "POST", body: '{"password":"secret"}' })]);
  expect(password.value).toBe("");
  expect(document.querySelector('[role="alert"]')?.textContent).toContain("login failed");
});

test("login duplicate submits are guarded and wrong password refocuses a cleared field", async () => {
  let release!: () => void;
  const fetch = vi.fn((url: string) => {
    if (url === "/api/admin/session") return Promise.resolve(ok({ authenticated: false }));
    return new Promise((resolve) => (release = () => resolve(bad(401, "no"))));
  });
  await render(fetch);
  const password = input("#password", "wrong");
  submit(password.form!);
  submit(password.form!);
  expect(fetch).toHaveBeenCalledTimes(2);
  release();
  await flush();
  expect(password.value).toBe("");
  expect(document.activeElement).toBe(password);
});

test("csrf protects create, duplicate create is ignored, and a 401 preserves the draft", async () => {
  const fetch = loggedInFetch();
  let release!: () => void;
  fetch.mockImplementation(async (url: string, init?: RequestInit) => {
    if (url === "/api/admin/session") return ok(session);
    if (url === "/api/admin/timeline") return new Promise((resolve) => (release = () => resolve(bad(401, "expired"))));
    if (url === "/api/timeline") return ok({ entries: [entry(1)], next_cursor: null });
    return ok({});
  });
  await render(fetch);
  const draft = input('[aria-label="new entry"]', "unsaved draft");
  const form = draft.form!;
  submit(form);
  submit(form);
  expect(fetch).toHaveBeenCalledTimes(3);
  expect(fetch.mock.calls[2][1]?.headers).toEqual(expect.objectContaining({ "X-CSRF-Token": "csrf" }));
  release();
  await flush();
  expect(document.querySelector<HTMLTextAreaElement>('[aria-label="new entry"]')?.value).toBe("unsaved draft");
  expect(document.body.textContent).toContain("session expired");
});

test("pagination deduplicates ids", async () => {
  const fetch = loggedInFetch([[entry(1), entry(2)], [entry(2), entry(3)]]);
  await render(fetch);
  await flush();
  click("load more");
  await flush();
  expect([...document.querySelectorAll(".timeline-entry")].map((e) => e.id)).toEqual(["entry-1", "entry-2", "entry-3"]);
});

test("create is guarded and edit saves the full replacement", async () => {
  const fetch = loggedInFetch();
  let release!: () => void;
  fetch.mockImplementation(async (url: string, init?: RequestInit) => {
    if (url === "/api/admin/session") return ok(session);
    if (url === "/api/timeline") return ok({ entries: [entry(1)], next_cursor: null });
    if (url === "/api/admin/timeline" && init?.method === "POST") return new Promise((resolve) => (release = () => resolve(ok(json(2, "new")))));
    if (url === "/api/admin/timeline/2" && init?.method === "GET") return ok(json(2, "old"));
    if (url === "/api/admin/timeline/2" && init?.method === "PATCH") return ok(json(2, "replacement"));
    return ok({});
  });
  await render(fetch);
  const compose = input('[aria-label="new entry"]', "new post");
  submit(compose.form!);
  submit(compose.form!);
  expect(fetch.mock.calls.filter(([url]) => url === "/api/admin/timeline")).toHaveLength(1);
  release();
  await flush();
  click("edit");
  await flush();
  const edit = input('[aria-label="edit entry"]', "replacement");
  submit(edit.form!);
  await flush();
  const patch = fetch.mock.calls.find(([url, init]) => url === "/api/admin/timeline/2" && init?.method === "PATCH");
  expect(patch?.[1]?.body).toBe('{"body_markdown":"replacement"}');
});

test("delete cancel preserves the entry, delete removes it and restores focus", async () => {
  const fetch = loggedInFetch();
  fetch.mockImplementation(async (url: string) => {
    if (url === "/api/admin/session") return ok(session);
    if (url === "/api/timeline") return ok({ entries: [entry(1), entry(2)], next_cursor: null });
    return ok({});
  });
  await render(fetch);
  const del = [...document.querySelectorAll("button")].find((b) => b.textContent?.startsWith("delete") && b.getAttribute("aria-busy") !== "true")!;
  act(() => del.focus());
  click("delete");
  expect((document.activeElement as HTMLButtonElement)?.textContent?.startsWith("cancel")).toBe(true);
  click("cancel");
  expect(document.querySelector("#entry-1")).not.toBeNull();
  click("delete");
  click("delete");
  await flush();
  expect(document.querySelector("#entry-1")).toBeNull();
  expect((document.activeElement as HTMLButtonElement)?.getAttribute("data-action")).toBe("edit");
});

test("429 uses Retry-After and the editor counts 10,000 Unicode code points", async () => {
  const headers = new Headers({ "Retry-After": "61" });
  const fetch = vi.fn(async (url: string) => url === "/api/admin/session" ? ok({ authenticated: false }) : bad(429, "slow", headers));
  await render(fetch);
  input("#password", "password");
  submit(document.querySelector("form")!);
  await flush();
  expect(document.body.textContent).toContain("try again in 2 min");
  // astral characters are two UTF-16 units but one server-counted code point.
  const logged = loggedInFetch();
  await render(logged);
  const text = "𐐷".repeat(10_000);
  const box = input('[aria-label="new entry"]', text) as HTMLTextAreaElement;
  expect(document.querySelector("#compose-count")?.textContent).toContain("10,000 / 10,000");
  expect([...document.querySelectorAll("button")].find((button) => button.textContent === "post")?.getAttribute("aria-disabled")).not.toBe("true");
  expect(box.value).toBe(text);
});

// Fake timers; the 5 s timeout below guards against a hang if a pending promise never settles.
async function renderPending(fetch: (url: string) => Promise<unknown>) {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  vi.stubGlobal("fetch", vi.fn(fetch));
  document.body.innerHTML = '<div id="root"></div>';
  const { default: Admin } = await import("./Admin");
  await act(async () => {
    root = createRoot(document.getElementById("root")!);
    root.render(<Admin />);
  });
}
const settle = () => act(async () => await Promise.resolve());
const wait = (ms: number) => act(() => vi.advanceTimersByTime(ms));
const pending = () => {
  let resolve!: (r: unknown) => void;
  return { promise: new Promise((r) => (resolve = r)), resolve: (r: unknown) => resolve(r) };
};

test("a fast session check shows no loading line; a slow one says loading after 400 ms", { timeout: 5000 }, async () => {
  try {
    const check = pending();
    await renderPending(() => check.promise);
    expect(document.querySelector("h1")?.textContent).toBe("admin");
    expect(document.body.textContent).not.toContain("loading");
    await wait(399);
    expect(document.body.textContent).not.toContain("loading");
    await wait(1);
    expect(document.querySelector('[role="status"]')?.textContent).toBe("loading");
    await act(async () => check.resolve(ok({ authenticated: false })));
    await settle();
    expect(document.querySelector('input[type="password"]')).not.toBeNull();
    expect(document.body.textContent).not.toContain("loading");
  } finally {
    vi.useRealTimers();
  }
});

test("retry after a 503 hides the loading line again for 400 ms", { timeout: 5000 }, async () => {
  try {
    const retry = pending();
    let calls = 0;
    await renderPending(() => (calls++ ? retry.promise : Promise.resolve(bad(503, "unavailable"))));
    await settle();
    expect(document.querySelector('[role="alert"]')?.textContent).toBe("unavailable, try again");
    click("retry");
    expect(document.body.textContent).not.toContain("loading");
    await wait(400);
    expect(document.querySelector('[role="status"]')?.textContent).toBe("loading");
    await act(async () => retry.resolve(ok({ authenticated: false })));
    await settle();
  } finally {
    vi.useRealTimers();
  }
});

// Attachments: generated in-memory files only.
const png = (name: string) => new File([new Uint8Array([0x89, 0x50, 0x4e, 0x47, name.length])], name, { type: "image/png" });
function previews() {
  let n = 0;
  const revoked: string[] = [];
  URL.createObjectURL = vi.fn(() => `blob:preview-${n++}`);
  URL.revokeObjectURL = vi.fn((url: string) => void revoked.push(url));
  return revoked;
}
const fileInput = () => {
  const field = document.querySelector<HTMLInputElement>("#compose-files");
  expect(field, "the compose form's file input").not.toBeNull();
  return field!;
};
const pick = (files: File[]) =>
  act(() => {
    const field = fileInput();
    Object.defineProperty(field, "files", { value: files, configurable: true });
    field.dispatchEvent(new Event("change", { bubbles: true }));
  });
const rows = () => document.querySelectorAll(".attachment");
const postButton = () => [...document.querySelectorAll("button")].find((b) => b.textContent?.startsWith("post"))!;
function mediaFetch(upload: (file: File, n: number) => Promise<unknown> = (_, n) => Promise.resolve(ok({ id: `id-${n}`, width: 4, height: 3 }))) {
  let n = 0;
  return vi.fn(async (url: string, init?: RequestInit) => {
    if (url === "/api/admin/session") return ok(session);
    if (url === "/api/timeline") return ok({ entries: [], next_cursor: null });
    if (url === "/api/admin/media") return upload(init?.body as File, n++);
    if (url === "/api/admin/timeline") return ok({ ...json(9, "with images"), media: [{ id: "id-0", width: 4, height: 3, alt: "a red square" }] });
    return ok({});
  });
}

test("the file input has a visible label and picking 2 files uploads 2 raw bodies with the CSRF header", async () => {
  previews();
  const fetch = mediaFetch();
  await render(fetch);
  const field = fileInput();
  expect(document.querySelector(`label[for="${field.id}"]`)?.textContent).toBe("attach images");
  expect(field.disabled).toBe(false);
  const files = [png("a.png"), png("bb.png")];
  pick(files);
  await flush();
  const uploads = fetch.mock.calls.filter(([url]) => url === "/api/admin/media");
  expect(uploads.map(([, init]) => init?.body)).toEqual(files);
  for (const [, init] of uploads) {
    expect(init?.method).toBe("POST");
    expect(init?.headers).toEqual(expect.objectContaining({ "X-CSRF-Token": "csrf", "Content-Type": "image/png" }));
  }
  expect(rows()).toHaveLength(2);
  expect([...document.querySelectorAll(".attachment img")].map((img) => img.getAttribute("src"))).toEqual(["blob:preview-0", "blob:preview-1"]);
});

test("a fifth image shows up to 4 images and uploads nothing more", async () => {
  previews();
  const fetch = mediaFetch();
  await render(fetch);
  pick([png("1"), png("2"), png("3"), png("4")]);
  await flush();
  pick([png("5")]);
  await flush();
  expect(rows()).toHaveLength(4);
  expect(fetch.mock.calls.filter(([url]) => url === "/api/admin/media")).toHaveLength(4);
  expect(document.querySelector("#compose-files-error")?.textContent).toBe("up to 4 images");
});

test.each([
  [413, "request too large"],
  [422, "unsupported image"],
])("an upload %s shows the server detail and drops the row", async (status, detail) => {
  const revoked = previews();
  await render(mediaFetch(() => Promise.resolve(bad(status, detail))));
  pick([png("x")]);
  await flush();
  expect(document.querySelector("#compose-files-error")?.textContent).toBe(detail);
  expect(rows()).toHaveLength(0);
  expect(revoked).toEqual(["blob:preview-0"]);
});

test("post sends attachments with alt in order, empty alt as an empty string, and revokes the previews", async () => {
  const revoked = previews();
  const fetch = mediaFetch();
  await render(fetch);
  pick([png("a"), png("b")]);
  await flush();
  input(".attachment:first-child input", "a red square");
  input('[aria-label="new entry"]', "two images");
  click("post");
  await flush();
  const post = fetch.mock.calls.find(([url]) => url === "/api/admin/timeline");
  expect(JSON.parse(post?.[1]?.body as string)).toEqual({
    body_markdown: "two images",
    attachments: [
      { id: "id-0", alt: "a red square" },
      { id: "id-1", alt: "" },
    ],
  });
  expect(rows()).toHaveLength(0);
  expect(revoked.sort()).toEqual(["blob:preview-0", "blob:preview-1"]);
  // The new entry in the list shows its image.
  expect(document.querySelector("#entry-9 .media img")?.getAttribute("alt")).toBe("a red square");
});

test("remove drops a row and revokes its preview", async () => {
  const revoked = previews();
  const fetch = mediaFetch();
  await render(fetch);
  pick([png("a"), png("b")]);
  await flush();
  click("remove");
  expect(rows()).toHaveLength(1);
  expect(revoked).toEqual(["blob:preview-0"]);
  input('[aria-label="new entry"]', "one image");
  click("post");
  await flush();
  const post = fetch.mock.calls.find(([url]) => url === "/api/admin/timeline");
  expect(JSON.parse(post?.[1]?.body as string).attachments).toEqual([{ id: "id-1", alt: "" }]);
});

test("post is disabled while an image is uploading", async () => {
  previews();
  let release!: () => void;
  const fetch = mediaFetch(() => new Promise((resolve) => (release = () => resolve(ok({ id: "late", width: 1, height: 1 })))));
  await render(fetch);
  const box = input('[aria-label="new entry"]', "waiting");
  pick([png("slow")]);
  await flush();
  expect(document.querySelector(".attachment [role=status]")?.textContent).toBe("uploading");
  expect(postButton().getAttribute("aria-disabled")).toBe("true");
  submit(box.form!);
  await flush();
  expect(fetch.mock.calls.filter(([url]) => url === "/api/admin/timeline")).toHaveLength(0);
  release();
  await flush();
  expect(document.querySelector(".attachment [role=status]")).toBeNull();
  expect(postButton().getAttribute("aria-disabled")).toBe("false");
});
