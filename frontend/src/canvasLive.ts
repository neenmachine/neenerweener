import { useEffect, useRef, useState, type RefObject } from "react";
import { fetchWithSignal } from "./request";

export type CanvasEvent = { revision: number; event_kind: "place" | "clear"; x?: number; y?: number; color?: number; handle?: string; fingerprint_suffix?: string; created_at?: string; clear_bounds?: { x: number; y: number; width: number; height: number } };
export type CanvasSnapshot = { width: 64; height: 64; palette: string[]; revision: number; pixels: string };
export type EventResponse = { reset: boolean; revision: number; events: CanvasEvent[] };
export type CanvasMode = { current: "live" | "replay" };

type Loaded<T> = { response: Response; data?: T };
type RequestFetch = (url: string, signal: AbortSignal, init?: RequestInit) => Promise<Response>;

export function createCanvasLiveRequests(mode: CanvasMode, request: RequestFetch = fetchWithSignal) {
  let generation = 0;
  let active: AbortController | undefined;

  const invalidate = () => {
    generation += 1;
    active?.abort();
    active = undefined;
  };
  const load = async <T,>(url: string, init?: RequestInit): Promise<Loaded<T> | undefined> => {
    invalidate();
    const requestGeneration = generation;
    const controller = new AbortController();
    active = controller;
    const isCurrent = () => generation === requestGeneration && !controller.signal.aborted && mode.current === "live";
    try {
      const response = await request(url, controller.signal, init);
      if (!isCurrent()) return undefined;
      if (!response.ok || response.status === 304) return { response };
      const data = await response.json() as T;
      if (!isCurrent()) return undefined;
      return { response, data };
    } catch (error) {
      if (!isCurrent()) return undefined;
      throw error;
    }
  };

  return { load, invalidate };
}

export function decodePixels(value: string): Uint8Array {
  const binary = atob(value);
  return Uint8Array.from(binary, (character) => character.charCodeAt(0));
}

export function applyCanvasEvents(pixels: Uint8Array, events: CanvasEvent[], width = 64): Uint8Array {
  const next = pixels.slice();
  for (const event of events) {
    if (event.event_kind === "clear" && event.clear_bounds) {
      const { x, y, width: w, height: h } = event.clear_bounds;
      for (let row = y; row < y + h; row += 1) for (let column = x; column < x + w; column += 1) next[row * width + column] = 255;
    } else if (event.event_kind === "place" && event.x !== undefined && event.y !== undefined && event.color !== undefined) {
      next[event.y * width + event.x] = event.color;
    }
  }
  return next;
}

export function drawCanvas(canvas: HTMLCanvasElement, snapshot: CanvasSnapshot, pixels: Uint8Array) {
  const context = canvas.getContext("2d");
  if (!context) return;
  const source = document.createElement("canvas");
  source.width = snapshot.width;
  source.height = snapshot.height;
  const sourceContext = source.getContext("2d");
  if (!sourceContext) return;
  const image = sourceContext.createImageData(snapshot.width, snapshot.height);
  for (let i = 0; i < pixels.length; i += 1) {
    const value = (snapshot.palette[pixels[i]] ?? "#000000").slice(1);
    image.data.set([parseInt(value.slice(0, 2), 16), parseInt(value.slice(2, 4), 16), parseInt(value.slice(4, 6), 16), pixels[i] === 255 ? 0 : 255], i * 4);
  }
  sourceContext.putImageData(image, 0, 0);
  context.imageSmoothingEnabled = false;
  context.clearRect(0, 0, canvas.width, canvas.height);
  context.drawImage(source, 0, 0, canvas.width, canvas.height);
}

function delay(milliseconds: number) {
  return new Promise<void>((resolve) => window.setTimeout(resolve, milliseconds));
}

export function useCanvasLive(initial: CanvasSnapshot, initialEvents: CanvasEvent[], mode: CanvasMode, canvas: RefObject<HTMLCanvasElement | null>) {
  const [snapshot, setSnapshot] = useState(initial);
  const [events, setEvents] = useState(initialEvents);
  const [status, setStatus] = useState("");
  const [newest, setNewest] = useState<CanvasEvent>();
  const snapshotRef = useRef(initial);
  const pixels = useRef(decodePixels(initial.pixels));
  const eventRevision = useRef(initial.revision);
  const etag = useRef<string | undefined>(undefined);
  const mounted = useRef(true);
  const needsSnapshot = useRef(false);
  const pollNow = useRef<() => void>(() => {});
  const pausePolling = useRef<() => void>(() => {});
  const requests = useRef(createCanvasLiveRequests(mode));

  const commitSnapshot = (next: CanvasSnapshot, resetCursor = true) => {
    snapshotRef.current = next;
    if (resetCursor) eventRevision.current = next.revision;
    pixels.current = decodePixels(next.pixels);
    needsSnapshot.current = false;
    if (canvas.current) drawCanvas(canvas.current, next, pixels.current);
    if (mounted.current) setSnapshot(next);
  };
  const commitEvents = (result: EventResponse, currentRevision: number) => {
    if (result.reset || (result.events.length > 0 && result.events[0].revision > currentRevision + 1)) {
      needsSnapshot.current = true;
      return false;
    }
    if (!result.events.length) return false;
    const nextPixels = applyCanvasEvents(pixels.current, result.events);
    pixels.current = nextPixels;
    eventRevision.current = result.events.at(-1)!.revision;
    const next = { ...snapshotRef.current, revision: Math.max(snapshotRef.current.revision, eventRevision.current), pixels: btoa(String.fromCharCode(...nextPixels)) };
    snapshotRef.current = next;
    if (canvas.current) drawCanvas(canvas.current, next, nextPixels);
    if (mounted.current) {
      setSnapshot(next);
      setEvents((current) => [...current, ...result.events].slice(-20));
      setNewest(result.events.at(-1));
    }
    return currentRevision < result.revision;
  };

  useEffect(() => {
    mounted.current = true;
    if (canvas.current) drawCanvas(canvas.current, snapshotRef.current, pixels.current);
    return () => { mounted.current = false; };
  }, [canvas]);

  useEffect(() => {
    let timer: number | undefined;
    let stopped = false;
    const clearTimer = () => { if (timer !== undefined) window.clearTimeout(timer); timer = undefined; };
    const schedule = (milliseconds: number) => { clearTimer(); timer = window.setTimeout(poll, milliseconds); };
    const poll = async () => {
      if (stopped || mode.current !== "live") return;
      try {
        let catchUp = false;
        if (needsSnapshot.current) {
          const loaded = await requests.current.load<CanvasSnapshot>("/api/canvas", { headers: etag.current ? { "If-None-Match": etag.current } : undefined });
          if (!loaded) return;
          if (loaded.response.status === 304) needsSnapshot.current = false;
          else {
            if (!loaded.response.ok || !loaded.data) throw new Error("snapshot failed");
            etag.current = loaded.response.headers?.get("ETag") ?? undefined;
            commitSnapshot(loaded.data);
          }
        } else {
          const currentRevision = eventRevision.current;
          const loaded = await requests.current.load<EventResponse>(`/api/canvas/events?after=${currentRevision}`);
          if (!loaded) return;
          if (!loaded.response.ok || !loaded.data) throw new Error("poll failed");
          catchUp = commitEvents(loaded.data, currentRevision);
        }
        if (mounted.current) setStatus("");
        schedule(needsSnapshot.current || catchUp ? 0 : 3000);
      } catch (error) {
        if (!stopped && mode.current === "live" && (error as Error).name !== "AbortError") {
          needsSnapshot.current = true;
          if (mounted.current) setStatus("live updates are unavailable; retrying");
          schedule(3000);
        }
      }
    };
    pollNow.current = () => { clearTimer(); void poll(); };
    pausePolling.current = () => { clearTimer(); requests.current.invalidate(); };
    const visibility = () => {
      if (document.visibilityState === "visible") pollNow.current();
      else pausePolling.current();
    };
    if (document.visibilityState === "visible") schedule(3000);
    document.addEventListener("visibilitychange", visibility);
    return () => {
      stopped = true;
      pausePolling.current();
      document.removeEventListener("visibilitychange", visibility);
    };
  }, [canvas, mode]);

  const enterReplay = () => {
    mode.current = "replay";
    pausePolling.current();
  };
  const restore = async () => {
    mode.current = "live";
    pausePolling.current();
    for (let attempt = 0; attempt < 3 && mounted.current && mode.current === "live"; attempt += 1) {
      try {
        const loaded = await requests.current.load<CanvasSnapshot>("/api/canvas");
        if (!loaded) return false;
        if (!loaded.response.ok || !loaded.data) throw new Error("restore failed");
        commitSnapshot(loaded.data, false);
        setStatus("");
        pollNow.current();
        return true;
      } catch (error) {
        if ((error as Error).name === "AbortError") return false;
        if (mounted.current) setStatus("live canvas restore failed; retrying");
        await delay(250 * (attempt + 1));
      }
    }
    if (mounted.current && mode.current === "live") {
      needsSnapshot.current = true;
      pollNow.current();
    }
    return false;
  };

  return { snapshot, events, status, newest, pixels, enterReplay, restore, reportStatus: setStatus, redraw: () => canvas.current && drawCanvas(canvas.current, snapshotRef.current, pixels.current) };
}
