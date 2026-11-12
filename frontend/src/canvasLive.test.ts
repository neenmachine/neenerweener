import { expect, test, vi } from "vitest";
import { createCanvasLiveRequests, type CanvasMode } from "./canvasLive";

type Value = { value: string; reset?: boolean };
type Pending<T> = { promise: Promise<T>; resolve(value: T): void };

function pending<T>(): Pending<T> {
  let resolve!: (value: T) => void;
  return { promise: new Promise<T>((done) => { resolve = done; }), resolve };
}
function response(data: Promise<Value>): Response {
  return { ok: true, status: 200, json: () => data } as Response;
}

const scenarios: { name: string; run(): Promise<void> }[] = [
  {
    name: "success",
    async run() {
      const mode: CanvasMode = { current: "live" };
      const requests = createCanvasLiveRequests(mode, async () => response(Promise.resolve({ value: "current" })));
      expect((await requests.load<Value>("/events"))?.data?.value).toBe("current");
    },
  },
  {
    name: "abort and replace before fetch resolves",
    async run() {
      const mode: CanvasMode = { current: "live" };
      const oldFetch = pending<Response>();
      const fetcher = vi.fn().mockReturnValueOnce(oldFetch.promise).mockResolvedValueOnce(response(Promise.resolve({ value: "replacement" })));
      const requests = createCanvasLiveRequests(mode, fetcher);
      const old = requests.load<Value>("/old");
      const replacement = requests.load<Value>("/replacement");
      oldFetch.resolve(response(Promise.resolve({ value: "stale" })));
      expect(await old).toBeUndefined();
      expect((await replacement)?.data?.value).toBe("replacement");
    },
  },
  {
    name: "abort and replace before json resolves",
    async run() {
      const mode: CanvasMode = { current: "live" };
      const oldJson = pending<Value>();
      const fetcher = vi.fn().mockResolvedValueOnce(response(oldJson.promise)).mockResolvedValueOnce(response(Promise.resolve({ value: "replacement" })));
      const requests = createCanvasLiveRequests(mode, fetcher);
      const old = requests.load<Value>("/old");
      await Promise.resolve();
      const replacement = requests.load<Value>("/replacement");
      oldJson.resolve({ value: "stale" });
      expect(await old).toBeUndefined();
      expect((await replacement)?.data?.value).toBe("replacement");
    },
  },
  {
    name: "replay starts mid-request",
    async run() {
      const mode: CanvasMode = { current: "live" };
      const json = pending<Value>();
      const requests = createCanvasLiveRequests(mode, async () => response(json.promise));
      const old = requests.load<Value>("/events");
      await Promise.resolve();
      mode.current = "replay";
      requests.invalidate();
      json.resolve({ value: "stale" });
      expect(await old).toBeUndefined();
    },
  },
  {
    name: "replay exit and restore reject stale-old-poll",
    async run() {
      const mode: CanvasMode = { current: "live" };
      const oldJson = pending<Value>();
      const restoreJson = pending<Value>();
      const fetcher = vi.fn().mockResolvedValueOnce(response(oldJson.promise)).mockResolvedValueOnce(response(restoreJson.promise));
      const requests = createCanvasLiveRequests(mode, fetcher);
      const old = requests.load<Value>("/events");
      await Promise.resolve();
      mode.current = "replay";
      requests.invalidate();
      mode.current = "live";
      const restore = requests.load<Value>("/canvas");
      restoreJson.resolve({ value: "restored" });
      oldJson.resolve({ value: "stale-old-poll" });
      const committed = [(await restore)?.data?.value, (await old)?.data?.value].filter(Boolean);
      expect(committed).toEqual(["restored"]);
      expect(committed).not.toContain("stale-old-poll");
    },
  },
  {
    name: "reset recovery installs a snapshot request",
    async run() {
      const mode: CanvasMode = { current: "live" };
      const fetcher = vi.fn()
        .mockResolvedValueOnce(response(Promise.resolve({ value: "reset", reset: true })))
        .mockResolvedValueOnce(response(Promise.resolve({ value: "snapshot" })));
      const requests = createCanvasLiveRequests(mode, fetcher);
      const reset = await requests.load<Value>("/events");
      const snapshot = reset?.data?.reset ? await requests.load<Value>("/canvas") : undefined;
      expect([reset?.data?.value, snapshot?.data?.value]).toEqual(["reset", "snapshot"]);
    },
  },
  {
    name: "visibility hide and show replace a pending request",
    async run() {
      const mode: CanvasMode = { current: "live" };
      const hiddenJson = pending<Value>();
      const fetcher = vi.fn().mockResolvedValueOnce(response(hiddenJson.promise)).mockResolvedValueOnce(response(Promise.resolve({ value: "visible" })));
      const requests = createCanvasLiveRequests(mode, fetcher);
      const hidden = requests.load<Value>("/events");
      await Promise.resolve();
      requests.invalidate();
      const visible = requests.load<Value>("/events");
      hiddenJson.resolve({ value: "hidden" });
      expect([(await hidden)?.data?.value, (await visible)?.data?.value].filter(Boolean)).toEqual(["visible"]);
    },
  },
  {
    name: "unmount invalidates a pending request",
    async run() {
      const mode: CanvasMode = { current: "live" };
      const json = pending<Value>();
      const requests = createCanvasLiveRequests(mode, async () => response(json.promise));
      const result = requests.load<Value>("/events");
      await Promise.resolve();
      requests.invalidate();
      json.resolve({ value: "after-unmount" });
      expect(await result).toBeUndefined();
    },
  },
  {
    name: "duplicate overlapping events keep only the newest request",
    async run() {
      const mode: CanvasMode = { current: "live" };
      const first = pending<Value>();
      const second = pending<Value>();
      const third = pending<Value>();
      const fetcher = vi.fn().mockResolvedValueOnce(response(first.promise)).mockResolvedValueOnce(response(second.promise)).mockResolvedValueOnce(response(third.promise));
      const requests = createCanvasLiveRequests(mode, fetcher);
      const loads = [requests.load<Value>("/events"), requests.load<Value>("/events"), requests.load<Value>("/events")];
      third.resolve({ value: "revision-3" });
      second.resolve({ value: "revision-2" });
      first.resolve({ value: "revision-1" });
      const values = (await Promise.all(loads)).map((loaded) => loaded?.data?.value).filter(Boolean);
      expect(values).toEqual(["revision-3"]);
    },
  },
];

test("live request ownership commits only the current live generation", async () => {
  for (const scenario of scenarios) await scenario.run();
});
