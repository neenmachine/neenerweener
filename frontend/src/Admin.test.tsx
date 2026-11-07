// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, expect, test, vi } from "vitest";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

type Entry = { id: number; html: string; created_at: string; edited_at: string | null };
const entry = (id: number, body = `entry ${id}`): Entry => ({ id, html: `<p>${body}</p>`, created_at: "2026-01-02T03:04:05Z", edited_at: null });
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
