import { useEffect, useMemo, useRef, useState } from "react";
import { fetchWithSignal } from "./request";

type Event = { revision: number; event_kind: "place" | "clear"; x?: number; y?: number; color?: number; handle?: string; fingerprint_suffix?: string; created_at?: string; clear_bounds?: { x: number; y: number; width: number; height: number } };
export type CanvasSnapshot = { width: 64; height: 64; palette: string[]; revision: number; pixels: string };
type EventResponse = { reset: boolean; revision: number; events: Event[] };

export function applyCanvasEvents(pixels: Uint8Array, events: Event[], width = 64): Uint8Array {
  const next = pixels.slice();
  for (const event of events) {
    if (event.event_kind === "clear" && event.clear_bounds) {
      const { x, y, width: w, height: h } = event.clear_bounds;
      for (let row = y; row < y + h; row += 1) for (let column = x; column < x + w; column += 1) next[row * width + column] = 255;
    } else if (event.event_kind === "place" && event.x !== undefined && event.y !== undefined && event.color !== undefined) next[event.y * width + event.x] = event.color;
  }
  return next;
}

export function replayPositionToRevision(history: Event[], position: number): number { return position > 0 ? history[Math.min(position, history.length) - 1]?.revision ?? 0 : 0; }
export function replayStep(position: number, delta: number, length: number): number { return Math.max(0, Math.min(length, position + delta)); }
export function replayDelay(speed: number): number { return 35 / speed; }

function decode(value: string): Uint8Array { const binary = atob(value); return Uint8Array.from(binary, (character) => character.charCodeAt(0)); }
function draw(canvas: HTMLCanvasElement, snapshot: CanvasSnapshot, pixels: Uint8Array) {
  const context = canvas.getContext("2d"); if (!context) return;
  const source = document.createElement("canvas"); source.width = snapshot.width; source.height = snapshot.height;
  const sourceContext = source.getContext("2d"); if (!sourceContext) return;
  const image = sourceContext.createImageData(snapshot.width, snapshot.height);
  for (let i = 0; i < pixels.length; i += 1) {
    const value = (snapshot.palette[pixels[i]] ?? "#000000").slice(1);
    image.data.set([parseInt(value.slice(0, 2), 16), parseInt(value.slice(2, 4), 16), parseInt(value.slice(4, 6), 16), pixels[i] === 255 ? 0 : 255], i * 4);
  }
  sourceContext.putImageData(image, 0, 0); context.imageSmoothingEnabled = false; context.clearRect(0, 0, canvas.width, canvas.height); context.drawImage(source, 0, 0, canvas.width, canvas.height);
}
export async function loadCanvasHistory(fetchPage: (after: number) => Promise<EventResponse>, signal: AbortSignal): Promise<Event[]> {
  let cursor = 0; const history: Event[] = [];
  while (!signal.aborted) { const result = await fetchPage(cursor); if (result.reset) return history; history.push(...result.events); if (!result.events.length) break; cursor = result.events[result.events.length - 1].revision; if (cursor >= result.revision) break; }
  return history;
}
function relativeTime(value?: string): string { if (!value) return "now"; const seconds = Math.max(0, (Date.now() - Date.parse(value)) / 1000); if (seconds < 5) return "now"; if (seconds < 60) return `${Math.floor(seconds)}s`; if (seconds < 3600) return `${Math.floor(seconds / 60)}m`; if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`; return new Intl.DateTimeFormat("en-CA", { month: "short", day: "numeric", timeZone: "America/Toronto" }).format(new Date(value)); }
function fullTime(value?: string): string { return value ? new Intl.DateTimeFormat("en-US", { year: "numeric", month: "long", day: "numeric", hour: "numeric", minute: "2-digit", timeZone: "America/Toronto", timeZoneName: "short" }).format(new Date(value)) : ""; }
function agent(event: Event): string { return event.handle || event.fingerprint_suffix ? event.handle ?? "unknown" : "unknown"; }
function isEmpty(pixels: Uint8Array) { return pixels.length > 0 && pixels.every((pixel) => pixel === 255); }

function ActivityRows({ events, palette, onHover }: { events: Event[]; palette: string[]; onHover: (event?: Event) => void }) {
  return <ol className="canvas-activity">{events.length ? events.slice().reverse().slice(0, 20).map((event) => <li key={event.revision} onMouseEnter={() => onHover(event)} onMouseLeave={() => onHover()} className={event.revision === events[events.length - 1]?.revision ? "is-new" : undefined}>
    <span className="activity-swatch" style={event.event_kind === "place" ? { backgroundColor: palette[event.color ?? 0] } : undefined} />
    <span className="activity-who"><b title={agent(event)}>{event.event_kind === "clear" ? "region cleared" : agent(event)}</b>{event.event_kind === "place" && <code>{event.fingerprint_suffix ?? ""}</code>}</span>
    <code className="activity-where">{event.event_kind === "clear" && event.clear_bounds ? `${event.clear_bounds.width}×${event.clear_bounds.height}` : `${event.x ?? 0},${event.y ?? 0}`}</code>
    <time dateTime={event.created_at} title={fullTime(event.created_at)}>{relativeTime(event.created_at)}</time>
  </li>) : <li className="muted">no activity yet</li>}</ol>;
}

export default function CanvasPage({ snapshot, initialEvents = [] }: { snapshot: CanvasSnapshot; initialEvents?: Event[] }) {
  const frame = useRef<HTMLDivElement>(null); const canvas = useRef<HTMLCanvasElement>(null); const toggle = useRef<HTMLButtonElement>(null); const close = useRef<HTMLButtonElement>(null);
  const pixels = useRef(decode(snapshot.pixels)); const revision = useRef(snapshot.revision); const [events, setEvents] = useState(initialEvents);
  const [status, setStatus] = useState(""); const [activity, setActivity] = useState(false); const [replayActive, setReplayActive] = useState(false); const [playing, setPlaying] = useState(false); const [history, setHistory] = useState<Event[]>([]); const [position, setPosition] = useState(0); const [speed, setSpeed] = useState(1); const [marker, setMarker] = useState<Event>();
  const replayingRef = useRef(false); const replayController = useRef<AbortController | undefined>(undefined); const restoreController = useRef<AbortController | undefined>(undefined); const pollNow = useRef<(() => void) | undefined>(undefined); const needsSnapshot = useRef(false); const reduced = useRef(false); const mounted = useRef(true); const timer = useRef<number | undefined>(undefined);
  const empty = isEmpty(pixels.current);
  const liveRevision = snapshot.revision;
  const drawAt = (at: number, list = history) => { const framePixels = applyCanvasEvents(new Uint8Array(snapshot.width * snapshot.height).fill(255), list.slice(0, at)); if (canvas.current) draw(canvas.current, snapshot, framePixels); };
  const stopReplay = async () => { replayController.current?.abort(); replayingRef.current = false; setPlaying(false); setReplayActive(false); const controller = new AbortController(); restoreController.current = controller; await restoreLive(controller); restoreController.current = undefined; pollNow.current?.(); };
  const restoreLive = async (controller: AbortController) => { for (let attempt = 0; attempt < 3 && !controller.signal.aborted; attempt += 1) try { const response = await fetchWithSignal("/api/canvas", controller.signal); if (!response.ok) throw new Error(); const live = await response.json() as CanvasSnapshot; if (!mounted.current || controller.signal.aborted) return; pixels.current = decode(live.pixels); revision.current = live.revision; if (canvas.current) draw(canvas.current, live, pixels.current); return; } catch (error) { if ((error as Error).name === "AbortError") return; setStatus("live canvas restore failed; retrying"); await new Promise((resolve) => window.setTimeout(resolve, 250 * (attempt + 1))); } };
  const startReplay = async () => {
    if (empty) return;
    if (replayActive) { await stopReplay(); return; }
    replayingRef.current = true; setReplayActive(true); setPlaying(!reduced.current); setPosition(0); setHistory([]); const controller = new AbortController(); replayController.current = controller;
    try { const loaded = await loadCanvasHistory(async (after) => { const response = await fetchWithSignal(`/api/canvas/history?after=${after}&limit=500`, controller.signal); if (!response.ok) throw new Error(); return await response.json() as EventResponse; }, controller.signal); if (!controller.signal.aborted) { setHistory(loaded); if (loaded.length && !reduced.current) setPosition(1); } }
    catch (error) { if ((error as Error).name !== "AbortError") setStatus("replay failed; retrying"); }
  };
  useEffect(() => { reduced.current = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false; if (canvas.current) draw(canvas.current, snapshot, pixels.current); const saved = window.innerWidth >= 900 && localStorage.getItem("canvas-activity") === "open"; if (saved) setActivity(true); return () => { mounted.current = false; replayController.current?.abort(); restoreController.current?.abort(); if (timer.current) clearTimeout(timer.current); }; }, [snapshot]);
  useEffect(() => { const resize = () => { const box = frame.current; if (!box) return; const rect = box.getBoundingClientRect(); const cell = Math.max(1, Math.floor(Math.min(rect.width, rect.height) / 64)); if (canvas.current) { canvas.current.width = canvas.current.height = cell * 64; canvas.current.style.width = `${cell * 64}px`; canvas.current.style.height = `${cell * 64}px`; draw(canvas.current, snapshot, pixels.current); } }; resize(); if (typeof ResizeObserver === "undefined") return; const observer = new ResizeObserver(resize); if (frame.current) observer.observe(frame.current); return () => observer.disconnect(); }, [snapshot, activity]);
  useEffect(() => { if (!activity) return; localStorage.setItem("canvas-activity", "open"); }, [activity]);
  useEffect(() => { if (!activity && window.innerWidth >= 900) localStorage.setItem("canvas-activity", "closed"); }, [activity]);
  useEffect(() => { if (activity && window.innerWidth < 900) close.current?.focus(); if (!activity) return; const key = (event: KeyboardEvent) => { if (event.key === "Escape") { setActivity(false); toggle.current?.focus(); } }; document.addEventListener("keydown", key); return () => document.removeEventListener("keydown", key); }, [activity]);
  useEffect(() => { let controller: AbortController | undefined; let stopped = false; const poll = async () => { if (stopped || replayingRef.current) return; controller?.abort(); controller = new AbortController(); try { const response = await fetchWithSignal(`/api/canvas/events?after=${revision.current}`, controller.signal); if (!response.ok) throw new Error(); const result = await response.json() as EventResponse; if (result.reset) needsSnapshot.current = true; else if (result.events.length) { pixels.current = applyCanvasEvents(pixels.current, result.events); revision.current = result.events[result.events.length - 1].revision; setEvents((current) => [...current, ...result.events].slice(-20)); setMarker(result.events[result.events.length - 1]); if (canvas.current) draw(canvas.current, snapshot, pixels.current); } setStatus(""); timer.current = window.setTimeout(poll, 3000); } catch (error) { if (!stopped && (error as Error).name !== "AbortError") { setStatus("live updates are unavailable; retrying"); timer.current = window.setTimeout(poll, 3000); } } }; pollNow.current = () => { if (timer.current) clearTimeout(timer.current); void poll(); }; timer.current = window.setTimeout(poll, 3000); return () => { stopped = true; controller?.abort(); if (timer.current) clearTimeout(timer.current); }; }, [snapshot]);
  useEffect(() => { if (!replayActive || !playing || !history.length) return; timer.current = window.setTimeout(() => setPosition((value) => { if (value >= history.length) { setPlaying(false); return value; } return value + 1; }), replayDelay(speed)); return () => { if (timer.current) clearTimeout(timer.current); }; }, [replayActive, playing, position, speed, history.length]);
  useEffect(() => { if (replayActive) { drawAt(position); } }, [position, history, replayActive]);
  const currentRevision = replayActive ? replayPositionToRevision(history, position) : revision.current;
  const seek = (next: number) => { setPlaying(false); setPosition(replayStep(next, 0, history.length)); };
  const cycleSpeed = () => setSpeed((value) => value === 1 ? 4 : value === 4 ? 16 : 1);
  const toggleActivity = () => setActivity((value) => !value);
  const markerStyle = marker?.event_kind === "place" && marker.x !== undefined && marker.y !== undefined ? { left: `${marker.x * 100 / 64}%`, top: `${marker.y * 100 / 64}%` } : undefined;
  const rows = useMemo(() => <ActivityRows events={events} palette={snapshot.palette} onHover={(event) => { if (!replayActive) setMarker(event); }} />, [events, snapshot.palette, replayActive]);
  return <main className="canvas-instrument" data-activity={activity ? "open" : "closed"}>
    <a className="canvas-home" href="/">neenerweener</a>
    <div className="canvas-pill">
      {!replayActive ? <><span className="canvas-live"><i /> live · rev {revision.current.toLocaleString()}</span><span className="canvas-divider" /><button className="canvas-pill-action" type="button" onClick={startReplay} aria-disabled={empty || undefined}><span className="play-icon" />replay</button><a className="canvas-pill-action" href={empty ? undefined : "/api/canvas.png"} download="neenerweener-canvas.png" aria-label="download png" aria-disabled={empty || undefined}><span className="download-icon" />png</a></> : <div className="canvas-transport" aria-busy={!history.length}>
        <button type="button" className="canvas-play" onClick={() => setPlaying((value) => !value)} aria-label={playing ? "pause replay" : "play replay"}><span className={playing ? "pause-icon" : "play-icon"} /></button>
        <input type="range" min="0" max={history.length} value={position} onChange={(event) => seek(Number(event.currentTarget.value))} aria-label="replay position" aria-valuetext={`rev ${currentRevision.toLocaleString()} of ${history.length.toLocaleString()}`} />
        <span className="canvas-position">rev {currentRevision.toLocaleString()} / {history.length.toLocaleString()}</span><button className="canvas-speed" type="button" onClick={cycleSpeed} aria-label={`replay speed, ${speed}×`}>{speed}×</button><button className="canvas-stop" type="button" onClick={stopReplay} aria-label="stop replay">×</button>
      </div>}
      <span className="canvas-divider" /><button ref={toggle} className="canvas-pill-action" type="button" aria-expanded={activity} aria-controls="canvas-activity" onClick={toggleActivity} aria-label={activity ? "hide activity" : "show activity"}><span className="activity-icon" /> <span className="activity-label">activity</span></button>
    </div>
    <div className="canvas-stage" ref={frame}><div className="canvas-frame"><img src="/api/canvas.png" width="512" height="512" alt="" aria-hidden="true" decoding="async" /><canvas ref={canvas} width="512" height="512" role="img" aria-label={replayActive ? `canvas at rev ${currentRevision} of ${history.length}` : "current canvas"} /><span className="canvas-marker" style={markerStyle} /></div>{empty && <span className="canvas-empty-label">no pixels yet</span>}<p className="canvas-status" role={status ? "status" : undefined} aria-live={status ? "polite" : undefined}><i className={replayActive ? "replay-dot" : ""} />{status || (replayActive ? `replay · rev ${currentRevision.toLocaleString()} of ${history.length.toLocaleString()}` : `live · rev ${revision.current.toLocaleString()}`)}</p></div>
    <aside id="canvas-activity" className="canvas-panel" aria-labelledby="canvas-activity-title" hidden={!activity}><header><h2 id="canvas-activity-title">recent activity</h2><button ref={close} type="button" onClick={() => { setActivity(false); toggle.current?.focus(); }} aria-label="close activity">×</button></header>{rows}</aside>
    <details className="canvas-nojs-activity"><summary>recent activity</summary>{rows}</details>
  </main>;
}
