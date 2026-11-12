// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, expect, test, vi } from "vitest";
import AdminAgents from "./AdminAgents";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const props = (send: any, expire = vi.fn(), active = true) => ({ send, expire, profile: null, loadProfile: vi.fn(), setProfile: vi.fn(), active });
let root: Root | undefined;
afterEach(() => { act(() => root?.unmount()); root = undefined; document.body.innerHTML = ""; });
const render = async (send: any, expire?: any, active = true) => {
  const node = document.createElement("div"); document.body.append(node);
  await act(async () => { root = createRoot(node); root.render(<AdminAgents {...props(send, expire, active)} />); await Promise.resolve(); });
};
const data = { agents: [{ id: "agent-1", handle: "alice", fingerprint_suffix: "1234", created_at: "2026-01-01T00:00:00Z", banned: false }], threads: [{ id: 1, title: "thread", handle: "a", hidden: false, deleted: false }], answers: [{ id: 1, thread_id: 9, handle: "b", hidden: false, deleted: false }], canvas: [], writes_enabled: true, audit: [{ action: "ban_agent", target: "agent-1", created_at: "2026-01-01T00:00:00Z" }] };
const ok = (data: unknown) => ({ ok: true, status: 200, data, message: "" });

test("initial 401 expires and reloads after login", async () => {
  const expire = vi.fn(); const send = vi.fn().mockResolvedValueOnce({ ok: false, status: 401, message: "expired" }).mockResolvedValueOnce(ok(data));
  await render(send, expire);
  await act(async () => { root!.render(<AdminAgents {...props(send, expire, false)} />); await Promise.resolve(); });
  await act(async () => { root!.render(<AdminAgents {...props(send, expire, true)} />); await Promise.resolve(); });
  expect(expire).toHaveBeenCalledOnce();
  expect(send).toHaveBeenCalledTimes(2);
  expect(document.body.textContent).toContain("alice");
});

test("moderation controls have target-specific names and banned state", async () => {
  await render(vi.fn().mockResolvedValue(ok(data)));
  expect(document.querySelector('[aria-label="ban alice"]')).not.toBeNull();
  expect(document.querySelector('[aria-label="delete thread 1"]')).not.toBeNull();
  expect(document.querySelector('[aria-label="delete answer 1"]')).not.toBeNull();
  expect(document.body.textContent).toContain("ban_agent");
  const ban = document.querySelector<HTMLButtonElement>('[aria-label="ban alice"]')!;
  act(() => ban.click());
  expect(document.querySelector('[aria-label="confirm ban alice"]')).not.toBeNull();
});

test("a held ban disables its control, drops a second click, then reloads", async () => {
  let release!: (value: any) => void;
  const held = new Promise((resolve) => (release = resolve));
  const send = vi.fn(async (_method: string, url: string) => url === "/api/admin/agents" ? ok(data) : held);
  await render(send);
  act(() => document.querySelector<HTMLButtonElement>('[aria-label="ban alice"]')!.click());
  await act(async () => { document.querySelector<HTMLButtonElement>('[aria-label="confirm ban alice"]')!.click(); await Promise.resolve(); });
  const button = document.querySelector<HTMLButtonElement>('[aria-label="confirm ban alice"]')!;
  expect(button.getAttribute("aria-busy")).toBe("true");
  expect(button.getAttribute("aria-disabled")).toBe("true");
  expect(button.textContent).toContain("banning");
  button.click();
  expect(send.mock.calls.filter(([, url]) => url !== "/api/admin/agents")).toHaveLength(1);
  await act(async () => { release(ok({})); await Promise.resolve(); await Promise.resolve(); });
  expect(button.getAttribute("aria-busy")).toBe("false");
});

test("successful ban reload shows banned state and unban", async () => {
  const banned = { ...data, agents: [{ ...data.agents[0], banned: true }] };
  const send = vi.fn().mockResolvedValueOnce(ok(data)).mockResolvedValueOnce(ok({})).mockResolvedValueOnce(ok(banned)).mockResolvedValue(ok(banned));
  await render(send);
  act(() => document.querySelector<HTMLButtonElement>('[aria-label="ban alice"]')!.click());
  await act(async () => { document.querySelector<HTMLButtonElement>('[aria-label="confirm ban alice"]')!.click(); await Promise.resolve(); });
  expect(document.body.textContent).toContain("alice · 1234 · banned");
  expect(document.querySelector('[aria-label="unban alice"]')).not.toBeNull();
});

test("delete confirmation includes forum kind", async () => {
  const send = vi.fn().mockResolvedValue(ok({ ...data, agents: [] }));
  await render(send);
  const deletes = [...document.querySelectorAll('button[aria-label^="delete "]')];
  expect(deletes).toHaveLength(2);
  act(() => (deletes[0] as HTMLButtonElement).click());
  expect(deletes[0].getAttribute("aria-label")).toBe("confirm delete thread 1");
  expect(deletes[1].getAttribute("aria-label")).toBe("delete answer 1");
});
