import { useEffect, useRef, useState } from "react";
import { fetchWithSignal } from "./request";

type Event = { revision: number; event_kind: "place" | "clear"; x?: number; y?: number; color?: number; clear_bounds?: { x: number; y: number; width: number; height: number } };
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
  const image = context.createImageData(snapshot.width, snapshot.height);
  for (let i = 0; i < pixels.length; i += 1) {
    const color = snapshot.palette[pixels[i]] ?? "#ffffff";
    const value = color.slice(1);
    image.data.set([parseInt(value.slice(0, 2), 16), parseInt(value.slice(2, 4), 16), parseInt(value.slice(4, 6), 16), pixels[i] === 255 ? 0 : 255], i * 4);
  }
  context.imageSmoothingEnabled = false;
  context.clearRect(0, 0, canvas.width, canvas.height);
  context.save();
  context.scale(canvas.width / snapshot.width, canvas.height / snapshot.height);
  context.putImageData(image, 0, 0);
  context.restore();
}

export default function CanvasPage({ snapshot, initialEvents = [] }: { snapshot: CanvasSnapshot; initialEvents?: Event[] }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const pixels = useRef(decode(snapshot.pixels));
  const revision = useRef(snapshot.revision);
  const [events, setEvents] = useState(initialEvents);
  const [status, setStatus] = useState("");
  const [replaying, setReplaying] = useState(false);
  const replayController = useRef<AbortController | undefined>(undefined);
  const reduced = useRef(false);

  useEffect(() => {
    reduced.current = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
    if (canvas.current) draw(canvas.current, snapshot, pixels.current);
  }, [snapshot]);

  useEffect(() => {
    let controller: AbortController | undefined;
    let timer: number | undefined;
    let stopped = false;
    const poll = async () => {
      controller?.abort();
      controller = new AbortController();
      try {
        const response = await fetchWithSignal(`/api/canvas/events?after=${revision.current}`, controller.signal);
        if (!response.ok) throw new Error("poll failed");
        const result = (await response.json()) as EventResponse;
        if (result.reset || (result.events.length > 0 && result.events[0].revision > revision.current + 1)) {
          const snapshotResponse = await fetchWithSignal("/api/canvas", controller.signal);
          const next = (await snapshotResponse.json()) as CanvasSnapshot;
          pixels.current = decode(next.pixels);
          revision.current = next.revision;
          if (canvas.current) draw(canvas.current, next, pixels.current);
        } else if (result.events.length) {
          pixels.current = applyCanvasEvents(pixels.current, result.events);
          revision.current = result.revision;
          setEvents((current) => [...current, ...result.events].slice(-20));
          if (canvas.current) draw(canvas.current, snapshot, pixels.current);
        }
        setStatus("");
      } catch (error) {
        if (!stopped && (error as Error).name !== "AbortError") setStatus("live updates are unavailable");
      } finally {
        if (!stopped && document.visibilityState === "visible") timer = window.setTimeout(poll, 3000);
      }
    };
    const visibility = () => { if (document.visibilityState === "visible") poll(); else { controller?.abort(); if (timer) window.clearTimeout(timer); } };
    if (document.visibilityState === "visible") timer = window.setTimeout(poll, 3000);
    document.addEventListener("visibilitychange", visibility);
    return () => { stopped = true; controller?.abort(); if (timer) window.clearTimeout(timer); document.removeEventListener("visibilitychange", visibility); };
  }, [snapshot]);

  const replay = async () => {
    if (replaying) { replayController.current?.abort(); return; }
    setReplaying(true);
    const controller = new AbortController();
    replayController.current = controller;
    try {
      const response = await fetchWithSignal(`/api/canvas/history?after=0&limit=500`, controller.signal);
      const result = (await response.json()) as EventResponse;
      if (reduced.current) {
        pixels.current = applyCanvasEvents(decode(snapshot.pixels), result.events);
        if (canvas.current) draw(canvas.current, snapshot, pixels.current);
      } else {
        const start = decode(snapshot.pixels);
        pixels.current = start;
        for (const event of result.events) { if (controller.signal.aborted) break; pixels.current = applyCanvasEvents(pixels.current, [event]); if (canvas.current) draw(canvas.current, snapshot, pixels.current); await new Promise((resolve) => window.setTimeout(resolve, 35)); }
      }
    } finally { controller.abort(); replayController.current = undefined; setReplaying(false); }
  };

  return <main><article className="canvas-page"><h1>canvas</h1><p className="muted">a shared 64 × 64 pixel canvas</p><div className="canvas-frame"><canvas ref={canvas} width={512} height={512} role="img" aria-label="current canvas" /></div><div className="canvas-controls"><button type="button" onClick={replay}>{replaying ? "stop replay" : "replay"}</button></div><p role="status" aria-live="polite">{status}</p><section aria-labelledby="canvas-activity"><h2 id="canvas-activity">recent activity</h2><ol className="canvas-activity">{events.length ? events.map((event) => <li key={event.revision}>revision {event.revision}: {event.event_kind === "clear" ? "cleared a region" : "pixel updated"}</li>) : <li className="muted">no activity yet</li>}</ol></section></article><Footer /></main>;
}

function Footer() { return <footer><a href="/">neenerweener</a><a href="/timeline">timeline</a><a href="/canvas" aria-current="page">canvas</a><a href="/feed.xml">rss</a></footer>; }
