// @vitest-environment jsdom
// The request lifecycle (useRequest) on every admin request path, driven through the real pages: one request per
// double activation, busy owned by the running request, newest load wins, and nothing runs after unmount.
import { act, StrictMode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, expect, test, vi } from "vitest";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

type Reply = { ok: boolean; status: number; headers: Headers; json: () => Promise<unknown> };
const reply = (status: number, body: unknown): Reply => ({ ok: status < 300, status, headers: new Headers(), json: () => Promise.resolve(body) });
const profile = { display_name: "fixture name", handle: null, avatar_id: null, avatar_initial: "F" };
const now = "2026-01-03T00:00:00+00:00";
const entry = (id: number) => ({ id, html: `<p>entry ${id}</p>`, created_at: now, created_zone: "EST", edited_at: null, edited_zone: null, media: [] });
const first = { entries: [entry(1)], next_cursor: null, now };

type Held = { url: string; method: string; signal: AbortSignal | undefined; answer: (r: Reply) => Promise<void> };
let root: Root | undefined;

afterEach(() => {
  document.cookie = "admin_hint=; Max-Age=0; Path=/";
  act(() => root?.unmount());
  root = undefined;
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.resetModules();
});

const flush = () => act(async () => await new Promise((resolve) => setTimeout(resolve, 0)));
const settle = async () => {
  for (let i = 0; i < 5; i++) await flush();
};

// Reads answer at once; every mutation (and any GET that hold() picks) waits in held until the test answers it.
async function render(
  path: string,
  { signedIn = true, strict = false, hold = (url: string, method: string) => method !== "GET" && url !== "/api/admin/logout" } = {},
) {
  const held: Held[] = [];
  const fetch = vi.fn((url: string, init: RequestInit = {}) => {
    const method = init.method ?? "GET";
    if (hold(url, method))
      return new Promise<Reply>((resolve) =>
        held.push({ url, method, signal: init.signal ?? undefined, answer: async (r) => (resolve(r), await settle()) }),
      );
    if (url === "/api/admin/session") return Promise.resolve(reply(200, signedIn ? { authenticated: true, csrf_token: "csrf" } : { authenticated: false }));
    if (url === "/api/admin/login") return Promise.resolve(reply(200, null));
    if (url === "/api/profile") return Promise.resolve(reply(200, profile));
    if (url === "/api/timeline") return Promise.resolve(reply(200, first));
    if (url === "/api/admin/overview") return Promise.resolve(reply(200, { entry_count: 1 }));
    if (url === "/api/admin/timeline/1") return Promise.resolve(reply(200, { ...entry(1), body_markdown: "entry 1" }));
    return Promise.resolve(reply(404, "x"));
  });
  vi.stubGlobal("fetch", fetch);
  history.replaceState(null, "", path);
  if (path === "/timeline") {
    document.cookie = "admin_hint=1; Path=/";
    const preload = { page: { kind: "timeline" }, "/api/timeline": first, "/api/profile": profile };
    document.body.innerHTML = `<script id="preload" type="application/json">${JSON.stringify(preload)}</script><div id="root"></div>`;
    await Promise.all([import("./TimelineComposer"), import("./EntryEditor")]);
  } else {
    document.body.innerHTML = `<script id="preload" type="application/json">{"page":{"kind":"admin"}}</script><div id="root"></div>`;
    await Promise.all([import("./Admin"), import("./AdminOverview"), import("./AdminTimeline"), import("./AdminProfile")]);
  }
  const { default: App } = await import("./App");
  await act(async () => {
    root = createRoot(document.getElementById("root")!);
    root.render(strict ? <StrictMode><App /></StrictMode> : <App />);
  });
  for (let i = 0; i < 20 && !document.querySelector("form, .admin-links"); i++)
    await act(async () => await new Promise((resolve) => setTimeout(resolve, 10)));
  await settle();
  return { fetch, held };
}

const visible = (selector: string) => [...document.querySelectorAll<HTMLElement>(selector)].filter((el) => !el.closest("[hidden]"));
const button = (name: string) =>
  visible("button").find((b) => b.getAttribute("aria-label") === name || b.textContent?.startsWith(name)) as HTMLButtonElement;
const type = (el: HTMLInputElement | HTMLTextAreaElement, value: string) =>
  act(() => {
    Object.getOwnPropertyDescriptor(el.constructor.prototype, "value")!.set!.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
  });
const cmdEnter = (el: Element) => el.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", metaKey: true, bubbles: true }));
// Two activations in one task, before React renders the first one's busy state.
const twice = (fn: () => void) => act(() => (fn(), fn()));
const textarea = (name: string) => document.querySelector<HTMLTextAreaElement>(`textarea[aria-label="${name}"]`)!;

// Each submit path: where it lives, how to get its form ready, the submit button, the field Enter submits from,
// and which request it sends.
type Path = {
  name: string;
  path: string;
  signedIn?: boolean;
  ready: () => Promise<void>;
  submit: string;
  field?: () => HTMLInputElement | HTMLTextAreaElement;
  sends: [string, string];
};
const paths: Path[] = [
  {
    name: "login",
    path: "/admin",
    signedIn: false,
    ready: async () => type(document.querySelector<HTMLInputElement>("#password")!, "secret"),
    submit: "log in",
    field: () => document.querySelector<HTMLInputElement>("#password")!,
    sends: ["POST", "/api/admin/login"],
  },
  {
    name: "admin create",
    path: "/admin/timeline",
    ready: async () => type(textarea("new entry"), "new post"),
    submit: "post",
    field: () => textarea("new entry"),
    sends: ["POST", "/api/admin/timeline"],
  },
  {
    name: "public create",
    path: "/timeline",
    ready: async () => {
      for (let i = 0; i < 100 && !textarea("new timeline entry"); i++)
        await act(async () => await new Promise((resolve) => setTimeout(resolve, 10)));
      await type(textarea("new timeline entry"), "new post");
    },
    submit: "tweet",
    field: () => textarea("new timeline entry"),
    sends: ["POST", "/api/admin/timeline"],
  },
  {
    name: "edit save",
    path: "/admin/timeline",
    ready: async () => {
      await act(() => button("edit entry 1").click());
      await settle();
      await type(textarea("edit entry 1"), "replacement");
    },
    submit: "save entry 1",
    field: () => textarea("edit entry 1"),
    sends: ["PATCH", "/api/admin/timeline/1"],
  },
  {
    name: "delete confirm",
    path: "/admin/timeline",
    ready: async () => act(() => button("delete entry 1").click()),
    submit: "confirm delete entry 1",
    sends: ["DELETE", "/api/admin/timeline/1"],
  },
  {
    name: "profile save",
    path: "/admin/profile",
    ready: async () => type(document.querySelector<HTMLInputElement>("#display-name")!, "new name"),
    submit: "save profile",
    field: () => document.querySelector<HTMLInputElement>("#display-name")!,
    sends: ["PATCH", "/api/admin/profile"],
  },
];

// A textarea submits on Cmd+Enter (its own handler); an input on Enter, which the browser turns into requestSubmit.
const enter = (field: HTMLInputElement | HTMLTextAreaElement) =>
  field instanceof HTMLTextAreaElement ? cmdEnter(field) : field.form!.requestSubmit();
const activations = paths.flatMap((p) => [
  { ...p, how: "double click", go: () => button(p.submit).click() },
  ...(p.field ? [{ ...p, how: "double Enter", go: () => enter(p.field!()) }] : []),
]);

test.each(activations)(
  "$name, $how: one request, busy stays on until that request settles, then goes off",
  { timeout: 5000 },
  async ({ path, signedIn, ready, submit, sends, go }) => {
    const { held } = await render(path, { signedIn });
    await ready();
    await twice(go);
    await settle();
    expect(held.map((h) => [h.method, h.url])).toEqual([sends]);
    // The second activation was dropped; it must not have cleared the first request's busy state.
    expect(button(submit).getAttribute("aria-busy")).toBe("true");
    await held[0].answer(reply(422, "held error"));
    expect(button(submit).getAttribute("aria-busy")).toBe("false");
  },
);

// The overview's count: two refreshes in flight, answered newest first.
const navLink = (label: string) => visible(".admin-nav a").find((a) => a.textContent === label)!;
const navigate = async (label: string) => {
  await act(() => navLink(label).dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, button: 0 })));
  await settle();
};
const count = () => visible(".admin-links a")[0]?.textContent;
const overview = (url: string) => url === "/api/admin/overview";

test("load, overview: a newer refresh supersedes the older, which is aborted and never applied", { timeout: 5000 }, async () => {
  const { held } = await render("/admin", { hold: overview });
  await held[0].answer(reply(200, { entry_count: 1 }));
  expect(count()).toBe("timeline1 entry");
  await navigate("timeline");
  await navigate("overview");
  await navigate("timeline");
  await navigate("overview");
  expect(held.map((h) => h.signal?.aborted)).toEqual([false, true, false]);
  await held[2].answer(reply(200, { entry_count: 2 }));
  expect(count()).toBe("timeline2 entries");
  await held[1].answer(reply(200, { entry_count: 7 }));
  expect(count()).toBe("timeline2 entries");
});

// Unmount (sign-out, leaving the page) with a request in flight: its fetch is aborted and, when the reply still
// comes, nothing of the caller's continuation runs: no object URL, no focus, no React error.
const unmountRows = [
  ...paths
    .filter((p) => p.field)
    .map((p) => ({ name: `${p.name} (submit)`, path: p.path, signedIn: p.signedIn, start: async () => (await p.ready(), act(() => enter(p.field!()))), answer: reply(422, "late") })),
  { name: "overview (load)", path: "/admin", signedIn: true, start: async () => {}, answer: reply(200, { entry_count: 5 }) },
  { name: "shell profile (load)", path: "/admin/profile", signedIn: true, start: async () => {}, answer: reply(200, profile) },
  {
    name: "profile picture upload (load)",
    path: "/admin/profile",
    signedIn: true,
    start: async () => {
      vi.spyOn(HTMLInputElement.prototype, "click").mockImplementation(() => {});
      await act(() => button("choose picture").click());
      const input = document.querySelector<HTMLInputElement>('input[type="file"]')!;
      Object.defineProperty(input, "files", { configurable: true, value: [new File(["x"], "fixture.png", { type: "image/png" })] });
      await act(async () => input.dispatchEvent(new Event("change", { bubbles: true })));
    },
    answer: reply(200, { id: "staged" }),
  },
];

test.each(unmountRows)("unmount while $name is pending: fetch aborted, no continuation after unmount", { timeout: 5000 }, async ({ name, path, signedIn, start, answer }) => {
  const { held } = await render(path, {
    signedIn,
    hold: (url, method) => (name.startsWith("overview") ? overview(url) : name.startsWith("shell profile") ? url === "/api/profile" : method !== "GET"),
  });
  await start();
  await settle();
  expect(held).toHaveLength(1);
  const error = vi.spyOn(console, "error");
  const focus = vi.spyOn(HTMLElement.prototype, "focus");
  const objectUrl = vi.fn(() => "blob:fixture");
  vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: objectUrl, revokeObjectURL: vi.fn() }));
  act(() => root!.unmount());
  root = undefined;
  expect(held[0].signal?.aborted).toBe(true);
  await held[0].answer(answer);
  expect(objectUrl).not.toHaveBeenCalled();
  expect(focus).not.toHaveBeenCalled();
  expect(error).not.toHaveBeenCalled();
});

// The shell's own flows, each its whole chain under one signal: logout, and login's session check after the password.
test("unmount while logout is pending: fetch aborted, no continuation after unmount", { timeout: 5000 }, async () => {
  const { held } = await render("/admin", { hold: (url) => url === "/api/admin/logout" });
  await act(() => button("log out").click());
  await settle();
  expect(held.map((h) => h.url)).toEqual(["/api/admin/logout"]);
  const error = vi.spyOn(console, "error");
  act(() => root!.unmount());
  root = undefined;
  expect(held[0].signal?.aborted).toBe(true);
  await held[0].answer(reply(204, null));
  expect(error).not.toHaveBeenCalled();
});

test("unmount while login's session check is pending: fetch aborted, no session applied, no profile load", { timeout: 5000 }, async () => {
  let sessions = 0;
  const { fetch, held } = await render("/admin", { signedIn: false, hold: (url) => url === "/api/admin/session" && ++sessions > 1 });
  await type(document.querySelector<HTMLInputElement>("#password")!, "secret");
  await act(() => button("log in").click());
  await settle();
  expect(held.map((h) => h.url)).toEqual(["/api/admin/session"]);
  const error = vi.spyOn(console, "error");
  act(() => root!.unmount());
  root = undefined;
  expect(held[0].signal?.aborted).toBe(true);
  const before = fetch.mock.calls.length;
  await held[0].answer(reply(200, { authenticated: true, csrf_token: "csrf" }));
  // session() applying the reply would start the profile load: no request at all may follow the unmount.
  expect(fetch.mock.calls.slice(before)).toEqual([]);
  expect(error).not.toHaveBeenCalled();
});

// Strict mode (as main.tsx renders) mounts the shell twice in dev: the first session check is aborted with it, and
// its late reply must not apply, or it would overwrite the second check's result.
test("strict mode replay: the aborted first session check never applies over the second", { timeout: 5000 }, async () => {
  const { held } = await render("/admin", { strict: true, hold: (url) => url === "/api/admin/session" });
  expect(held.map((h) => h.signal?.aborted)).toEqual([true, false]);
  await held[1].answer(reply(200, { authenticated: true, csrf_token: "csrf" }));
  expect(button("log out")).toBeDefined();
  await held[0].answer(reply(200, { authenticated: true, csrf_token: "csrf" }));
  expect(button("log out")).toBeDefined();
  expect(button("retry")).toBeUndefined();
});
