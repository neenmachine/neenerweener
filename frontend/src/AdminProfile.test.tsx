// @vitest-environment jsdom
// The profile page: the form, staged picture changes, save, errors and focus, and the shell identity after a save.
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, expect, test, vi } from "vitest";
import type { Profile } from "./timelineTypes";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// Vitest hands CSS imports over empty, so the sheet is read from disk; a string specifier keeps node types out of tsc.
const fs = "node:fs";
const css: string = (await import(/* @vite-ignore */ fs)).readFileSync("src/index.css", "utf8");
let root: Root | undefined;

afterEach(() => {
  act(() => root?.unmount());
  root = undefined;
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.resetModules();
});

type Reply = { ok: boolean; status: number; headers: Headers; json: () => Promise<unknown> };
const ok = (body: unknown): Reply => ({ ok: true, status: 200, headers: new Headers(), json: () => Promise.resolve(body) });
const bad = (status: number, detail: unknown): Reply => ({ ok: false, status, headers: new Headers(), json: () => Promise.resolve({ detail }) });
const fixture = { display_name: "fixture name", handle: "fixture_handle", avatar_id: null, avatar_initial: "F" };
const configured = { ...fixture, avatar_id: "00000000-0000-4000-8000-00000000000a" };
const flush = () => act(async () => await new Promise((resolve) => setTimeout(resolve, 0)));

type Call = { url: string; method: string; body: unknown };
// uploads and saves answer in order from the given lists; the default save echoes the body back as the profile.
function server(profile: Profile = fixture, { uploads = [] as Reply[], saves = [] as Reply[] } = {}) {
  const calls: Call[] = [];
  let signedIn = true;
  const fetch = vi.fn(async (url: string, init: RequestInit = {}) => {
    const method = init.method ?? "GET";
    const body = typeof init.body === "string" ? JSON.parse(init.body) : init.body;
    calls.push({ url, method, body });
    if (url === "/api/admin/session") return ok(signedIn ? { authenticated: true, csrf_token: "csrf" } : { authenticated: false });
    if (url === "/api/admin/login" || url === "/api/admin/logout")
      return (signedIn = url.endsWith("login")), { ok: true, status: 204, headers: new Headers(), json: () => Promise.reject() };
    if (url === "/api/profile") return ok(profile);
    if (url === "/api/admin/overview") return ok({ entry_count: 3 });
    if (url === "/api/admin/profile/avatar") return uploads.shift() ?? ok({ id: `staged-${calls.length}` });
    if (url === "/api/admin/profile") {
      const b = body as { display_name: string; handle: string | null; avatar_id?: string | null };
      const avatar_id = "avatar_id" in b ? b.avatar_id : profile.avatar_id;
      return saves.shift() ?? ok({ display_name: b.display_name.trim(), handle: b.handle, avatar_id, avatar_initial: b.display_name.trim()[0].toUpperCase() });
    }
    return bad(404, "x");
  });
  return { fetch, calls, mutations: () => calls.filter((c) => c.method !== "GET" && c.url.startsWith("/api/admin/profile")) };
}

async function render(s = server(), path = "/admin/profile") {
  vi.stubGlobal("fetch", s.fetch);
  let n = 0;
  vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: () => `blob:fixture-${++n}`, revokeObjectURL: vi.fn() }));
  history.replaceState(null, "", path);
  document.body.innerHTML = `<script id="preload" type="application/json">{"page":{"kind":"admin"}}</script><div id="root"></div>`;
  await Promise.all([import("./Admin"), import("./AdminOverview"), import("./AdminTimeline"), import("./AdminProfile")]);
  const { default: App } = await import("./App");
  await act(async () => {
    root = createRoot(document.getElementById("root")!);
    root.render(<App />);
  });
  for (let i = 0; i < 4; i++) await flush();
  return s;
}

const page = () => document.querySelector<HTMLElement>(".admin-page:not([hidden])")!;
const button = (label: string) => [...page().querySelectorAll("button")].find((b) => b.textContent?.startsWith(label))!;
const buttons = () => [...page().querySelectorAll("button")].map((b) => b.querySelector("span:not([hidden])")!.textContent);
const field = (id: string) => document.getElementById(id) as HTMLInputElement;
const alertText = () => page().querySelector("#profile-error")!.textContent;
const statusText = () => page().querySelector('[role="status"]')!.textContent;
const preview = () => page().querySelector<HTMLElement>(".picture .avatar")!;

async function type(input: HTMLInputElement, value: string) {
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}
async function click(el: HTMLElement) {
  await act(async () => el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, button: 0 })));
  for (let i = 0; i < 3; i++) await flush();
}
// choose/replace picture opens the hidden file input; this answers it with one file.
async function pickFile(label: string, name = "fixture.png") {
  const picker = vi.spyOn(HTMLInputElement.prototype, "click").mockImplementation(() => {});
  await click(button(label));
  expect(picker).toHaveBeenCalledTimes(1);
  picker.mockRestore();
  const input = page().querySelector<HTMLInputElement>('input[type="file"]')!;
  Object.defineProperty(input, "files", { configurable: true, value: [new File(["x"], name, { type: "image/png" })] });
  await act(async () => input.dispatchEvent(new Event("change", { bubbles: true })));
  for (let i = 0; i < 3; i++) await flush();
}
async function save() {
  const b = button("save profile");
  b.focus();
  await click(b);
}

test("shows the profile: one h1, the picture group, both fields and the actions", async () => {
  await render();
  expect([...page().querySelectorAll("h1, h2")].map((h) => h.textContent)).toEqual(["profile"]);
  const group = page().querySelector('[role="group"]')!;
  expect(group.getAttribute("aria-label")).toBe("profile picture");
  expect(preview().textContent).toBe("F");
  expect(preview().className).toBe("avatar preview");
  expect(field("display-name").value).toBe("fixture name");
  expect(field("handle").value).toBe("fixture_handle");
  expect(document.querySelector('label[for="display-name"]')!.textContent).toBe("display name");
  expect(document.querySelector('label[for="handle"]')!.textContent).toBe("handle");
  expect(buttons()).toEqual(["choose picture", "save profile"]);
});

test("a configured picture shows the server image with replace and remove", async () => {
  await render(server(configured));
  const img = preview() as HTMLImageElement;
  expect(img.tagName).toBe("IMG");
  expect(img.getAttribute("srcset")).toBe(`/media/${configured.avatar_id}/sm.webp 96w, /media/${configured.avatar_id}/lg.webp 192w`);
  expect(img.getAttribute("aria-hidden")).toBe("true");
  expect(buttons()).toEqual(["replace picture", "remove picture", "save profile"]);
});

test("the preview is 88px, and 72px under 760px", () => {
  const rule = (block: string) => /\.avatar\.preview\s*\{([^}]*)\}/.exec(block)?.[1] ?? "";
  const desktop = rule(css);
  expect(desktop).toMatch(/width:\s*88px/);
  expect(desktop).toMatch(/height:\s*88px/);
  const phone = [...css.matchAll(/@media \(max-width: 760px\) \{([\s\S]*?)\n\}/g)].map((m) => rule(m[1])).find(Boolean) ?? "";
  expect(phone).toMatch(/width:\s*72px/);
  expect(phone).toMatch(/height:\s*72px/);
});

test("the @ sits inside the handle border, outside the value and the payload", async () => {
  const s = await render();
  const wrapper = field("handle").parentElement!;
  expect(wrapper.className).toBe("handle-field");
  expect(wrapper.firstElementChild!.textContent).toBe("@");
  expect(wrapper.firstElementChild!.getAttribute("aria-hidden")).toBe("true");
  expect(/\.handle-field\s*\{[^}]*border:\s*1px solid/.test(css)).toBe(true);
  expect(/\.handle-field input\s*\{[^}]*border:\s*none/.test(css)).toBe(true);
  await save();
  expect(s.mutations()).toEqual([
    { url: "/api/admin/profile", method: "PATCH", body: { display_name: "fixture name", handle: "fixture_handle" } },
  ]);
});

test("save sends the fields, keeps focus, says profile saved, and the shell and overview show the new identity", async () => {
  const s = await render();
  await type(field("display-name"), "  other name ");
  await type(field("handle"), "");
  expect(s.mutations()).toEqual([]);
  await save();
  expect(s.mutations()).toEqual([{ url: "/api/admin/profile", method: "PATCH", body: { display_name: "  other name ", handle: null } }]);
  expect(document.activeElement).toBe(button("save profile"));
  expect(statusText()).toBe("profile saved");
  expect(field("display-name").value).toBe("other name");
  expect(document.querySelector(".admin-session .identity")!.textContent).toBe("Oother name");
  await click([...document.querySelectorAll<HTMLAnchorElement>("nav.admin-nav a")].find((a) => a.textContent === "overview")!);
  const row = [...page().querySelectorAll(".admin-links a")][1];
  expect(row.textContent).toBe("profileOother name");
  // No second profile fetch: the saved one was used.
  expect(s.calls.filter((c) => c.url === "/api/profile")).toHaveLength(1);
});

test("after a save, logging out and in again shows the saved profile, not the first fetch", async () => {
  const s = await render();
  await type(field("display-name"), "saved name");
  await save();
  await click([...document.querySelectorAll("button")].find((b) => b.textContent?.startsWith("log out"))!);
  const password = document.querySelector<HTMLInputElement>("#password")!;
  password.value = "fixture";
  await act(async () => password.form!.dispatchEvent(new SubmitEvent("submit", { bubbles: true, cancelable: true })));
  for (let i = 0; i < 4; i++) await flush();
  expect(field("display-name").value).toBe("saved name");
  expect(document.querySelector(".admin-session .identity")!.textContent).toBe("Ssaved name");
  expect(s.calls.filter((c) => c.url === "/api/profile")).toHaveLength(1);
});

test("choose picture uploads at once but changes the profile only on save", async () => {
  const s = await render();
  await pickFile("choose picture");
  expect(s.mutations()).toEqual([{ url: "/api/admin/profile/avatar", method: "POST", body: expect.any(File) }]);
  expect((preview() as HTMLImageElement).getAttribute("src")).toBe("blob:fixture-1");
  expect(buttons()).toEqual(["replace picture", "remove picture", "save profile"]);
  expect(document.querySelector(".admin-session .identity")!.textContent).toBe("Ffixture name");
  await save();
  expect(s.mutations()[1].body).toEqual({ display_name: "fixture name", handle: "fixture_handle", avatar_id: "staged-3" });
  expect(document.querySelector<HTMLImageElement>(".admin-session img.avatar")!.getAttribute("src")).toBe("/media/staged-3/sm.webp");
  expect(preview().getAttribute("src")).toBe("/media/staged-3/sm.webp");
});

test("replace stages the newer upload; remove stages null and moves focus to the picture button", async () => {
  const s = await render(server(configured));
  await pickFile("replace picture");
  await pickFile("replace picture");
  expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:fixture-1");
  await save();
  expect(s.mutations().map((c) => c.url)).toEqual(["/api/admin/profile/avatar", "/api/admin/profile/avatar", "/api/admin/profile"]);
  expect((s.mutations()[2].body as { avatar_id: string }).avatar_id).toBe("staged-4");

  await click(button("remove picture"));
  expect(preview().textContent).toBe("F");
  expect(buttons()).toEqual(["choose picture", "save profile"]);
  expect(document.activeElement).toBe(button("choose picture"));
  expect(s.mutations()).toHaveLength(3);
  await save();
  expect(s.mutations()[3].body).toEqual({ display_name: "fixture name", handle: "fixture_handle", avatar_id: null });
});

test("an upload error shows the server detail, focuses the picture button and keeps staged values", async () => {
  const s = await render(server(fixture, { uploads: [ok({ id: "first" }), bad(422, "unsupported image")] }));
  await type(field("display-name"), "draft name");
  await pickFile("choose picture");
  await pickFile("replace picture");
  expect(alertText()).toBe("unsupported image");
  expect(document.activeElement).toBe(button("replace picture"));
  expect(field("display-name").value).toBe("draft name");
  expect(preview().getAttribute("src")).toBe("blob:fixture-1");
  await save();
  expect(s.mutations()[2].body).toEqual({ display_name: "draft name", handle: "fixture_handle", avatar_id: "first" });
});

test.each([
  ["handle", "display-name", "fixture name", "handle", "bad-handle"],
  ["display name", "display-name", " ", "handle", "fixture_handle"],
])("an invalid %s: the server detail shows and focus goes to that field", async (_, nameId, name, handleId, handle) => {
  const s = await render(server(fixture, { saves: [bad(422, "invalid profile")] }));
  await pickFile("choose picture");
  await type(field(nameId), name);
  await type(field(handleId), handle);
  await save();
  const target = handle === "bad-handle" ? field("handle") : field("display-name");
  expect(alertText()).toBe("invalid profile");
  expect(document.activeElement).toBe(target);
  expect(target.getAttribute("aria-invalid")).toBe("true");
  expect(field("display-name").value).toBe(name);
  expect(field("handle").value).toBe(handle);
  expect(preview().getAttribute("src")).toBe("blob:fixture-1");
  expect(statusText()).toBe("");
  // Editing clears the mark; the retry still carries the staged picture.
  await type(target, target === field("handle") ? "good" : "good name");
  expect(target.getAttribute("aria-invalid")).toBeNull();
  await save();
  expect((s.mutations().at(-1)!.body as { avatar_id: string }).avatar_id).toBe("staged-3");
});

test("an unavailable server keeps the form and the focus on save", async () => {
  await render(server(fixture, { saves: [bad(503, "admin unavailable")] }));
  await type(field("handle"), "kept");
  await save();
  expect(alertText()).toBe("unavailable, try again");
  expect(document.activeElement).toBe(button("save profile"));
  expect(field("handle").value).toBe("kept");
  expect(document.querySelector(".admin-session .identity")!.textContent).toBe("Ffixture name");
});

test("while saving, the button says saving and a second save is ignored", async () => {
  let answer: (r: Reply) => void = () => {};
  const s = await render(server(fixture, { saves: [] }));
  s.fetch.mockImplementationOnce(() => new Promise<Reply>((resolve) => (answer = resolve)));
  await save();
  expect(button("save profile").getAttribute("aria-busy")).toBe("true");
  expect(button("save profile").querySelector("span:not([hidden])")!.textContent).toBe("saving");
  await click(button("save profile"));
  expect(s.fetch).toHaveBeenCalledTimes(s.calls.length + 1);
  await act(async () => answer(ok(fixture)));
  await flush();
  expect(statusText()).toBe("profile saved");
});
