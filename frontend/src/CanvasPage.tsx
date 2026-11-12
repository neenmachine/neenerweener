import { useEffect, useRef, useState } from "react";
import { fetchWithSignal } from "./request";

type Event = { revision: number; event_kind: "place" | "clear"; x?: number; y?: number; color?: number; agent?: string; fingerprint_suffix?: string; clear_bounds?: { x: number; y: number; width: number; height: number } };
export type CanvasSnapshot = { width: 64; height: 64; palette: string[]; revision: number; pixels: string };
type EventResponse = { reset: boolean; revision: number; events: Event[] };

export function applyCanvasEvents(pixels: Uint8Array, events: Event[], width = 64): Uint8Array {
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

function decode(value: string): Uint8Array {
  const binary = atob(value);
  return Uint8Array.from(binary, (character) => character.charCodeAt(0));
}

function draw(canvas: HTMLCanvasElement, snapshot: CanvasSnapshot, pixels: Uint8Array) {
  const context = canvas.getContext("2d");
  if (!context) return;
  const source = document.createElement("canvas");
  source.width = snapshot.width;
  source.height = snapshot.height;
  const sourceContext = source.getContext("2d");
  if (!sourceContext) return;
  const image = sourceContext.createImageData(snapshot.width, snapshot.height);
  for (let i = 0; i < pixels.length; i += 1) {
    const color = snapshot.palette[pixels[i]] ?? "#000000";
    const value = color.slice(1);
    image.data.set([parseInt(value.slice(0, 2), 16), parseInt(value.slice(2, 4), 16), parseInt(value.slice(4, 6), 16), pixels[i] === 255 ? 0 : 255], i * 4);
  }
  sourceContext.putImageData(image, 0, 0);
  context.imageSmoothingEnabled = false;
  context.clearRect(0, 0, canvas.width, canvas.height);
  context.drawImage(source, 0, 0, canvas.width, canvas.height);
}

export async function loadCanvasHistory(fetchPage: (after: number) => Promise<EventResponse>, signal: AbortSignal): Promise<Event[]> {
  let cursor = 0;
  let target = 0;
  const history: Event[] = [];
  do {
    const result = await fetchPage(cursor);
    target = Math.max(target, result.revision);
    if (result.reset) return history;
    history.push(...result.events);
    if (!result.events.length) break;
    cursor = result.events[result.events.length - 1].revision;
  } while (!signal.aborted && cursor < target);
  return history;
}

function delay(signal: AbortSignal, milliseconds: number) {
  return new Promise<void>((resolve) => {
    const timer = window.setTimeout(resolve, milliseconds);
    signal.addEventListener("abort", () => { window.clearTimeout(timer); resolve(); }, { once: true });
  });
}

export default function CanvasPage({ snapshot, initialEvents = [] }: { snapshot: CanvasSnapshot; initialEvents?: Event[] }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const pixels = useRef(decode(snapshot.pixels));
  const revision = useRef(snapshot.revision);
  const [events, setEvents] = useState(initialEvents);
  const [status, setStatus] = useState("");
  const [replaying, setReplaying] = useState(false);
  const replayingRef = useRef(false);
  const replayController = useRef<AbortController | undefined>(undefined);
  const pollNow = useRef<(() => void) | undefined>(undefined);
  const reduced = useRef(false);
  const mounted = useRef(true);

  useEffect(() => {
    reduced.current = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
    if (canvas.current) draw(canvas.current, snapshot, pixels.current);
    return () => { mounted.current = false; };
  }, [snapshot]);

  useEffect(() => {
    let controller: AbortController | undefined;
    let timer: number | undefined;
    let stopped = false;
    const poll = async () => {
      if (stopped || replayingRef.current) return;
      controller?.abort();
      controller = new AbortController();
      try {
        const response = await fetchWithSignal(`/api/canvas/events?after=${revision.current}`, controller.signal);
        if (!response.ok) throw new Error("poll failed");
        const result = (await response.json()) as EventResponse;
        if (replayingRef.current || stopped) return;
        if (result.reset || (result.events.length > 0 && result.events[0].revision > revision.current + 1)) {
          const snapshotResponse = await fetchWithSignal("/api/canvas", controller.signal);
          const next = (await snapshotResponse.json()) as CanvasSnapshot;
          if (replayingRef.current || stopped) return;
          pixels.current = decode(next.pixels);
          revision.current = next.revision;
          if (canvas.current) draw(canvas.current, next, pixels.current);
        } else if (result.events.length) {
          pixels.current = applyCanvasEvents(pixels.current, result.events);
          revision.current = result.events[result.events.length - 1].revision;
          if (mounted.current) setEvents((current) => [...current, ...result.events].slice(-20));
          if (canvas.current) draw(canvas.current, snapshot, pixels.current);
        }
        if (mounted.current) setStatus("");
        timer = result.events.length && revision.current < result.revision ? window.setTimeout(poll, 0) : window.setTimeout(poll, 3000);
      } catch (error) {
        if (!stopped && !replayingRef.current && (error as Error).name !== "AbortError") {
          if (mounted.current) setStatus("live updates are unavailable");
          timer = window.setTimeout(poll, 3000);
        }
      }
    };
    pollNow.current = poll;
    const visibility = () => { if (document.visibilityState === "visible") poll(); else { controller?.abort(); if (timer) window.clearTimeout(timer); } };
    if (document.visibilityState === "visible") timer = window.setTimeout(poll, 3000);
    document.addEventListener("visibilitychange", visibility);
    return () => { stopped = true; controller?.abort(); if (timer) window.clearTimeout(timer); pollNow.current = undefined; document.removeEventListener("visibilitychange", visibility); };
  }, [snapshot]);

  const replay = async () => {
    if (replayingRef.current) { replayController.current?.abort(); return; }
    replayingRef.current = true;
    setReplaying(true);
    const controller = new AbortController();
    replayController.current = controller;
    try {
      let replayPixels: Uint8Array = new Uint8Array(snapshot.width * snapshot.height).fill(255);
      if (canvas.current) draw(canvas.current, snapshot, replayPixels);
      const history = await loadCanvasHistory(
        async (after) => {
          const response = await fetchWithSignal(`/api/canvas/history?after=${after}&limit=500`, controller.signal);
          if (!response.ok) throw new Error("history failed");
          return (await response.json()) as EventResponse;
        },
        controller.signal,
      );
      for (const event of history) {
        if (controller.signal.aborted) return;
        replayPixels = applyCanvasEvents(replayPixels, [event]);
        if (!reduced.current && canvas.current) { draw(canvas.current, snapshot, replayPixels); await delay(controller.signal, 35); }
      }
      if (!controller.signal.aborted && reduced.current && canvas.current) draw(canvas.current, snapshot, replayPixels);
    } finally {
      controller.abort();
      replayController.current = undefined;
      replayingRef.current = false;
      if (mounted.current) {
        try {
          const response = await fetchWithSignal("/api/canvas", new AbortController().signal);
          if (response.ok) {
            const live = (await response.json()) as CanvasSnapshot;
            pixels.current = decode(live.pixels);
            revision.current = live.revision;
            if (canvas.current) draw(canvas.current, live, pixels.current);
          }
        } catch { /* restore is best effort; the next poll will retry */ }
        if (mounted.current) { setReplaying(false); pollNow.current?.(); }
      }
    }
  };

  return <main><article className="canvas-page"><h1>canvas</h1><p className="muted">a shared 64 × 64 pixel canvas</p><div className="canvas-frame"><canvas ref={canvas} width={512} height={512} role="img" aria-label="current canvas" /></div><div className="canvas-controls"><button type="button" onClick={replay}>{replaying ? "stop replay" : "replay"}</button></div><p role="status" aria-live="polite">{status}</p><section aria-labelledby="canvas-activity"><h2 id="canvas-activity">recent activity</h2><ol className="canvas-activity">{events.length ? events.map((event) => <li key={event.revision}>revision {event.revision}: {event.event_kind === "clear" ? "cleared a region" : "pixel updated"}{event.agent ? ` by ${event.agent}` : event.fingerprint_suffix ? ` by ${event.fingerprint_suffix}` : ""}</li>) : <li className="muted">no activity yet</li>}</ol></section></article><Footer /></main>;
}

function Footer() { return <footer><a href="/">neenerweener</a><a href="/timeline">timeline</a><a href="/canvas" aria-current="page">canvas</a><a href="/feed.xml">rss</a></footer>; }
