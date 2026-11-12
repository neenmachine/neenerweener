// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, expect, test, vi } from "vitest";
import AdminAgents from "./AdminAgents";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const props = (send: any, expire = vi.fn()) => ({ send, expire, profile: null, loadProfile: vi.fn(), setProfile: vi.fn(), active: true });
let root: Root | undefined;
afterEach(() => { act(() => root?.unmount()); root = undefined; document.body.innerHTML = ""; });
const render = async (send: any, expire?: any) => {
  const node = document.createElement("div"); document.body.append(node);
  await act(async () => { root = createRoot(node); root.render(<AdminAgents {...props(send, expire)} />); await Promise.resolve(); });
};
const data = { agents: [], threads: [{ id: 1, title: "thread", handle: "a", hidden: false, deleted: false }], answers: [{ id: 1, thread_id: 9, handle: "b", hidden: false, deleted: false }], canvas: [], writes_enabled: true };
const ok = (data: unknown) => ({ ok: true, status: 200, data, message: "" });

test("initial 401 expires the session", async () => {
  const expire = vi.fn();
  await render(vi.fn().mockResolvedValue({ ok: false, status: 401, message: "expired" }), expire);
  expect(expire).toHaveBeenCalledOnce();
});

test("delete confirmation includes forum kind", async () => {
  const send = vi.fn().mockResolvedValue(ok(data));
  await render(send);
  const deletes = [...document.querySelectorAll("button")].filter((button) => button.textContent?.startsWith("confirm action"));
  expect(deletes).toHaveLength(2);
  act(() => deletes[0].click());
  expect(deletes[0].querySelector("span:not([hidden])")?.textContent).toBe("confirm");
  expect(deletes[1].querySelector("span:not([hidden])")?.textContent).toBe("confirm action");
});
