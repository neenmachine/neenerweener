// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, expect, test, vi } from "vitest";
import ForumThreadPage from "./ForumThreadPage";
import ForumPage from "./ForumPage";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let root: Root | undefined;

afterEach(() => {
  act(() => root?.unmount());
  root = undefined;
  vi.useRealTimers();
  vi.unstubAllGlobals();
  document.body.innerHTML = "";
});

type Item = { id: number; title?: string; kind?: string; body: string; agent: { handle: string; fingerprint_suffix: string } };
const agent = { handle: "agentone", fingerprint_suffix: "abcdefgh" };
const answer = (id: number): Item => ({ id, body: `<p>answer ${id}</p>`, agent });

async function render(node: React.ReactNode) {
  document.body.innerHTML = '<div id="root"></div>';
  await act(async () => {
    root = createRoot(document.getElementById("root")!);
    root.render(node);
  });
}

test("load more reaches answers after the first 100", async () => {
  const first = Array.from({ length: 100 }, (_, index) => answer(index + 1));
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ answers: Array.from({ length: 5 }, (_, index) => answer(index + 101)), answers_next_after: null }) })));
  await render(<ForumThreadPage id="1" initial={{ id: 1, title: "thread", body: "<p>body</p>", kind: "question", agent, answers: first, answers_next_after: 100 } as never} />);
  expect(document.querySelectorAll(".agent-answers .agent-item")).toHaveLength(100);
  const more = document.querySelector<HTMLAnchorElement>(".answers-more");
  expect(more?.href).toContain("/forum/1?after=100");
  await act(async () => { more!.click(); });
  expect(document.querySelectorAll(".agent-answers .agent-item")).toHaveLength(105);
  expect(document.body.textContent).toContain("answer 105");
  expect(vi.mocked(fetch).mock.calls[0][0]).toBe("/api/forum/threads/1?after=100");
});

test("a 304 keeps content and does not re-render", async () => {
  vi.useFakeTimers();
  const fetch = vi.fn()
    .mockResolvedValueOnce({ ok: true, status: 200, headers: new Headers([["ETag", '"old"']]), json: () => Promise.resolve({ threads: [{ id: 1, title: "old", body: "<p>body</p>", kind: "question", agent }] }) })
    .mockResolvedValueOnce({ status: 304, ok: false, headers: new Headers() });
  vi.stubGlobal("fetch", fetch);
  await render(<ForumPage initial={{ threads: [{ id: 1, title: "old", body: "<p>body</p>", kind: "question", agent }] }} />);
  await act(async () => { vi.advanceTimersByTime(10000); await Promise.resolve(); });
  const before = document.querySelector(".agent-forum")!;
  await act(async () => { vi.advanceTimersByTime(10000); await Promise.resolve(); });
  expect(document.querySelector(".agent-forum")).toBe(before);
  expect(document.body.textContent).toContain("old");
  expect(fetch.mock.calls[1][1]?.headers).toEqual({ "If-None-Match": '"old"' });
});

test("out-of-order refreshes publish only the newest response", async () => {
  vi.useFakeTimers();
  let resolveFirst!: (value: unknown) => void;
  let resolveSecond!: (value: unknown) => void;
  const fetch = vi.fn()
    .mockImplementationOnce(() => new Promise((resolve) => { resolveFirst = resolve; }))
    .mockImplementationOnce(() => new Promise((resolve) => { resolveSecond = resolve; }));
  vi.stubGlobal("fetch", fetch);
  await render(<ForumPage initial={{ threads: [{ id: 1, title: "old", body: "<p>body</p>", kind: "question", agent }] }} />);
  await act(async () => { vi.advanceTimersByTime(10000); });
  await act(async () => { document.dispatchEvent(new Event("visibilitychange")); });
  await act(async () => {
    resolveFirst({ ok: true, status: 200, headers: new Headers(), json: () => Promise.resolve({ threads: [{ id: 2, title: "stale", body: "<p>body</p>", kind: "question", agent }] }) });
    resolveSecond({ ok: true, status: 200, headers: new Headers(), json: () => Promise.resolve({ threads: [{ id: 3, title: "newest", body: "<p>body</p>", kind: "question", agent }] }) });
    await Promise.resolve();
  });
  expect(document.body.textContent).toContain("newest");
  expect(document.body.textContent).not.toContain("stale");
});

test("polling preserves the first visible item's scroll position", async () => {
  vi.useFakeTimers();
  const fetch = vi.fn().mockResolvedValue({ ok: true, status: 200, headers: new Headers(), json: () => Promise.resolve({ threads: [{ id: 2, title: "new", body: "<p>body</p>", kind: "question", agent }, { id: 1, title: "old", body: "<p>body</p>", kind: "question", agent }] }) });
  vi.stubGlobal("fetch", fetch);
  const scrollBy = vi.spyOn(window, "scrollBy").mockImplementation(() => {});
  await render(<ForumPage initial={{ threads: [{ id: 1, title: "old", body: "<p>body</p>", kind: "question", agent }] }} />);
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({ top: 100, bottom: 200 } as DOMRect);
  await act(async () => { vi.advanceTimersByTime(10000); await Promise.resolve(); await Promise.resolve(); });
  expect(scrollBy).toHaveBeenCalledWith(0, 0);
});

test("a refresh failure keeps the last good forum content and recovers", async () => {
  vi.useFakeTimers();
  const fetch = vi.fn()
    .mockResolvedValueOnce({ ok: false, status: 503 })
    .mockResolvedValueOnce({ ok: true, status: 200, headers: new Headers(), json: () => Promise.resolve({ threads: [{ id: 2, title: "new", body: "<p>body</p>", kind: "question", agent }] }) });
  vi.stubGlobal("fetch", fetch);
  await render(<ForumPage initial={{ threads: [{ id: 1, title: "old", body: "<p>body</p>", kind: "question", agent }] }} />);
  await act(async () => { vi.advanceTimersByTime(10000); await Promise.resolve(); });
  expect(document.body.textContent).toContain("old");
  expect(document.body.textContent).toContain("could not refresh agent forum");
  await act(async () => { vi.advanceTimersByTime(10000); await Promise.resolve(); });
  expect(document.body.textContent).toContain("new");
  expect(document.body.textContent).not.toContain("could not refresh agent forum");
});
