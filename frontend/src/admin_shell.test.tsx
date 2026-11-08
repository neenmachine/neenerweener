// @vitest-environment jsdom
// The admin shell: the page registry, signed-out deep links, nav, history, titles and focus, and the overview page.
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, expect, test, vi } from "vitest";
import cases from "./route_cases.json";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | undefined;

afterEach(() => {
  act(() => root?.unmount());
  root = undefined;
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
  vi.resetModules();
});

const ok = (body: unknown) => ({ ok: true, status: 200, headers: new Headers(), json: () => Promise.resolve(body) });
const bad = (status: number) => ({ ok: false, status, headers: new Headers(), json: () => Promise.resolve({ detail: "x" }) });
const profile = { display_name: "fixture name", handle: "fixture_handle", avatar_id: null, avatar_initial: "F" };
const entry = { id: 1, html: "<p>fixture entry</p>", created_at: "2026-01-02T03:04:05Z", created_zone: "EST", edited_at: null, edited_zone: null, media: [] };
const flush = () => act(async () => await new Promise((resolve) => setTimeout(resolve, 0)));

type Answers = { signedIn?: boolean; count?: number | (() => unknown); entries?: unknown[] };
function server({ signedIn = true, count = 12, entries = [entry] }: Answers = {}) {
  let session = signedIn;
  return vi.fn(async (url: string) => {
    if (url === "/api/admin/session") return ok(session ? { authenticated: true, csrf_token: "csrf" } : { authenticated: false });
    if (url === "/api/admin/login") return (session = true), { ok: true, status: 204, headers: new Headers(), json: () => Promise.reject() };
    if (url === "/api/admin/logout") return (session = false), { ok: true, status: 204, headers: new Headers(), json: () => Promise.reject() };
    if (url === "/api/profile") return ok(profile);
    if (url === "/api/admin/overview") return typeof count === "function" ? count() : ok({ entry_count: count });
    if (url === "/api/timeline") return ok({ entries, next_cursor: null, now: "2026-01-03T00:00:00Z" });
    return bad(404);
  });
}

async function render(path: string, fetch = server()) {
  vi.stubGlobal("fetch", fetch);
  history.replaceState(null, "", path);
  document.title = "server title";
  document.body.innerHTML = `<script id="preload" type="application/json">{"page":{"kind":"admin"}}</script><div id="root"></div>`;
  // The lazy chunks, loaded up front so each page renders within the waits.
  await Promise.all([import("./Admin"), import("./AdminOverview"), import("./AdminTimeline"), import("./AdminProfile")]);
  const { default: App } = await import("./App");
  await act(async () => {
    root = createRoot(document.getElementById("root")!);
    root.render(<App />);
  });
  for (let i = 0; i < 4; i++) await flush();
  return fetch;
}

const visible = (selector: string) => [...document.querySelectorAll<HTMLElement>(selector)].filter((el) => !el.closest("[hidden]"));
const h1s = () => visible("h1").map((h) => h.textContent);
const navLink = (label: string) => [...document.querySelectorAll<HTMLAnchorElement>("nav.admin-nav a")].find((a) => a.textContent === label)!;
const current = () => [...document.querySelectorAll("nav.admin-nav a[aria-current=page]")].map((a) => a.textContent);
const click = async (el: HTMLElement) => {
  await act(async () => el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, button: 0 })));
  for (let i = 0; i < 3; i++) await flush();
};

test("the client page registry equals the shared admin_pages rows", async () => {
  const { adminPages } = await import("./adminPages");
  expect(adminPages.map(({ path, label }) => ({ path, label }))).toEqual(cases.admin_pages);
});

test("the nav lists every registered page in order and marks the shown one", async () => {
  await render("/admin/timeline");
  expect([...document.querySelectorAll("nav.admin-nav li a")].map((a) => [a.textContent, a.getAttribute("href")])).toEqual(
    cases.admin_pages.map((p) => [p.label, p.path]),
  );
  expect(current()).toEqual(["timeline"]);
  expect(h1s()).toEqual(["timeline"]);
});

test("the loaded admin page keeps one server robots tag", { timeout: 5000 }, async () => {
  document.head.innerHTML = '<meta name="robots" content="noindex">';
  await render("/admin", server({ signedIn: false }));
  const main = document.querySelector("main")!;
  const shell = main.firstElementChild!;
  expect(shell.matches("article.admin")).toBe(true);
  expect(shell.querySelector("h1")?.textContent).toBe("admin");
  expect(document.head.querySelectorAll('meta[name="robots"]').length).toBe(1);
  const footer = main.querySelector("footer")!;
  expect(shell.compareDocumentPosition(footer) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
});

test("signed out, a deep link shows sign in without nav, then the requested page after login", async () => {
  await render("/admin/profile", server({ signedIn: false }));
  expect(h1s()).toEqual(["admin"]);
  expect(visible("h2").map((h) => h.textContent)).toEqual(["sign in"]);
  expect(document.querySelector("nav")).toBeNull();
  const password = document.querySelector<HTMLInputElement>("#password")!;
  password.value = "fixture";
  await act(async () => password.form!.dispatchEvent(new SubmitEvent("submit", { bubbles: true, cancelable: true })));
  for (let i = 0; i < 4; i++) await flush();
  expect(location.pathname).toBe("/admin/profile");
  expect(h1s()).toEqual(["profile"]);
  expect(current()).toEqual(["profile"]);
});

test("a nav click swaps the page in place: url, title, current link and focused h1", async () => {
  const fetch = await render("/admin");
  expect(h1s()).toEqual(["overview"]);
  // Timeline content belongs to the timeline page only.
  expect(document.querySelector('[aria-label="new entry"]')).toBeNull();
  const push = vi.spyOn(history, "pushState");
  await click(navLink("timeline"));
  expect(location.pathname).toBe("/admin/timeline");
  expect(document.title).toBe("timeline · admin");
  expect(current()).toEqual(["timeline"]);
  expect(h1s()).toEqual(["timeline"]);
  expect(document.activeElement).toBe(visible("h1")[0]);
  expect(visible('[aria-label="new entry"]')).toHaveLength(1);
  // No document load: the session was checked once.
  expect(fetch.mock.calls.filter(([url]) => url === "/api/admin/session")).toHaveLength(1);

  // The current link does nothing.
  const textarea = visible('[aria-label="new entry"]')[0];
  textarea.focus();
  await click(navLink("timeline"));
  expect(push).toHaveBeenCalledTimes(1);
  expect(document.activeElement).toBe(textarea);
});

test("back restores the overview, its title and its focused h1", async () => {
  await render("/admin");
  await click(navLink("profile"));
  expect(document.title).toBe("profile · admin");
  await act(async () => {
    history.back();
    await new Promise((resolve) => setTimeout(resolve, 20));
  });
  await flush();
  expect(location.pathname).toBe("/admin");
  expect(document.title).toBe("overview · admin");
  expect(current()).toEqual(["overview"]);
  expect(h1s()).toEqual(["overview"]);
  expect(document.activeElement).toBe(visible("h1")[0]);
});

test("a modified click is left to the browser", async () => {
  await render("/admin");
  const event = new MouseEvent("click", { bubbles: true, cancelable: true, button: 0, metaKey: true });
  // jsdom cannot open a new tab; this runs after the app's handler, which must not have taken the click.
  document.addEventListener("click", (e) => (expect(e.defaultPrevented).toBe(false), e.preventDefault()), { once: true });
  await act(async () => navLink("timeline").dispatchEvent(event));
  expect(location.pathname).toBe("/admin");
});

test.each([
  [12, "12 entries"],
  [1, "1 entry"],
  [0, "0 entries"],
  [1234, "1,234 entries"],
])("the overview links timeline with %i as %s, and profile with the poster", async (count, text) => {
  await render("/admin", server({ count }));
  const rows = visible(".admin-links a");
  expect(rows.map((a) => a.getAttribute("href"))).toEqual(["/admin/timeline", "/admin/profile"]);
  expect(rows[0].textContent).toBe(`timeline${text}`);
  expect(rows[1].textContent).toBe("profileFfixture name@fixture_handle");
  await click(rows[0]);
  expect(location.pathname).toBe("/admin/timeline");
  expect(h1s()).toEqual(["timeline"]);
});

test("a failed overview count shows the error and retry loads it again", async () => {
  let fail = true;
  await render("/admin", server({ count: () => (fail ? bad(503) : ok({ entry_count: 2 })) }));
  expect(visible('[role="alert"]').map((p) => p.textContent)).toContain("unavailable, try again");
  expect(visible(".admin-links")).toHaveLength(0);
  fail = false;
  await click([...document.querySelectorAll("button")].find((b) => b.textContent?.startsWith("retry"))!);
  expect(visible(".admin-links a").map((a) => a.textContent)[0]).toBe("timeline2 entries");
});

test("an empty timeline says nothing here yet under entries", async () => {
  await render("/admin/timeline", server({ entries: [] }));
  expect(visible("h2").map((h) => h.textContent)).toEqual(["new entry", "entries"]);
  const entries = visible("h2")[1].parentElement!;
  expect(entries.textContent).toContain("nothing here yet");
});

test("the shell shows the poster and log out; log out keeps the url and drops the pages", async () => {
  await render("/admin/timeline");
  expect(document.querySelector(".admin-session .identity")!.textContent).toBe("Ffixture name");
  expect(document.querySelector(".admin-session button")!.textContent).toMatch(/^log out/);
  const draft = visible('[aria-label="new entry"]')[0] as HTMLTextAreaElement;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(draft, "unsaved");
    draft.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await click([...document.querySelectorAll("button")].find((b) => b.textContent?.startsWith("log out"))!);
  expect(location.pathname).toBe("/admin/timeline");
  expect(h1s()).toEqual(["admin"]);
  expect(document.querySelector('[aria-label="new entry"]')).toBeNull();
});
