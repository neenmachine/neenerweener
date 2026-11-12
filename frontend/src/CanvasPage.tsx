import { useEffect, useLayoutEffect, useMemo, useRef, useState, type Dispatch, type RefObject, type SetStateAction } from "react";
import { applyCanvasEvents, type CanvasEvent, type CanvasSnapshot, useCanvasLive } from "./canvasLive";
import { loadCanvasHistory, replayDelay, replayPositionToRevision, replayStep, useCanvasMode, useCanvasReplay } from "./canvasReplay";

export { applyCanvasEvents, loadCanvasHistory, replayDelay, replayPositionToRevision, replayStep };
export type { CanvasSnapshot };

function relativeTime(value: string | undefined, now: number): string {
  if (!value) return "now";
  const seconds = Math.max(0, (now - Date.parse(value)) / 1000);
  if (seconds < 5) return "now";
  if (seconds < 60) return `${Math.floor(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
  return new Intl.DateTimeFormat("en-CA", { month: "short", day: "numeric", timeZone: "America/Toronto" }).format(new Date(value)).toLowerCase();
}
function fullTime(value?: string): string {
  return value ? new Intl.DateTimeFormat("en-US", { year: "numeric", month: "long", day: "numeric", hour: "numeric", minute: "2-digit", timeZone: "America/Toronto", timeZoneName: "short" }).format(new Date(value)) : "";
}
function agent(event: CanvasEvent): string { return event.handle ?? "unknown"; }
function isEmpty(snapshot: CanvasSnapshot) { return !snapshot.pixels || atob(snapshot.pixels).split("").every((pixel) => pixel.charCodeAt(0) === 255); }
export function canvasSize(width: number, height: number) { return Math.max(64, Math.floor(Math.min(width, height) / 64) * 64); }

function useCanvasFrameSize(stage: RefObject<HTMLDivElement | null>, frame: RefObject<HTMLDivElement | null>) {
  useLayoutEffect(() => {
    const resize = () => {
      if (!stage.current?.clientWidth || !stage.current.clientHeight || !frame.current) return;
      const size = canvasSize(stage.current.clientWidth, stage.current.clientHeight);
      frame.current.style.width = `${size}px`;
      frame.current.style.height = `${size}px`;
    };
    resize();
    const observer = typeof ResizeObserver === "undefined" ? undefined : new ResizeObserver(resize);
    if (stage.current) observer?.observe(stage.current);
    window.addEventListener("resize", resize);
    return () => { observer?.disconnect(); window.removeEventListener("resize", resize); };
  }, [frame, stage]);
}

function useActivityModal(activity: boolean, setActivity: Dispatch<SetStateAction<boolean>>, mobile: boolean, toggle: RefObject<HTMLButtonElement | null>, close: RefObject<HTMLButtonElement | null>) {
  const wasOpen = useRef(false);
  useLayoutEffect(() => {
    if (wasOpen.current && !activity) toggle.current?.focus();
    wasOpen.current = activity;
  }, [activity, toggle]);
  useEffect(() => {
    if (!activity || !mobile) return;
    close.current?.focus();
    const trap = (event: KeyboardEvent) => {
      if (event.key === "Escape") { event.preventDefault(); setActivity(false); return; }
      if (event.key !== "Tab") return;
      const controls = [...document.querySelectorAll<HTMLElement>("#canvas-activity button, #canvas-activity a, #canvas-activity input")].filter((node) => !node.hasAttribute("disabled"));
      if (!controls.length) return;
      const first = controls[0], last = controls.at(-1)!;
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    document.addEventListener("keydown", trap);
    return () => document.removeEventListener("keydown", trap);
  }, [activity, close, mobile, setActivity]);
}

function canvasMarkerStyle(marker?: CanvasEvent) {
  if (marker?.event_kind === "place" && marker.x !== undefined && marker.y !== undefined) return { left: `calc(${marker.x} * 100% / 64 - 4px)`, top: `calc(${marker.y} * 100% / 64 - 4px)` };
  if (marker?.event_kind !== "clear" || !marker.clear_bounds) return undefined;
  const { x, y, width, height } = marker.clear_bounds;
  return { left: `calc(${x} * 100% / 64)`, top: `calc(${y} * 100% / 64)`, width: `calc(${width} * 100% / 64)`, height: `calc(${height} * 100% / 64)` };
}

function ActivityRow({ event, palette, newest, now, onHover }: { event: CanvasEvent; palette: string[]; newest?: number; now: number; onHover: (event?: CanvasEvent) => void }) {
  const clear = event.event_kind === "clear";
  return <li onMouseEnter={() => onHover(event)} onMouseLeave={() => onHover()} className={event.revision === newest ? "is-new" : undefined}>
    <span className="activity-swatch" style={!clear ? { backgroundColor: palette[event.color ?? 0] } : undefined} />
    <span className="activity-who"><b title={agent(event)}>{clear ? "region cleared" : agent(event)}</b>{!clear && <code>{event.fingerprint_suffix ?? ""}</code>}</span>
    <code className="activity-where">{clear && event.clear_bounds ? `${event.clear_bounds.width}×${event.clear_bounds.height}` : `${event.x ?? 0},${event.y ?? 0}`}</code>
    <time dateTime={event.created_at} title={fullTime(event.created_at)}>{relativeTime(event.created_at, now)}</time>
  </li>;
}

function ActivityRows({ events, palette, newest, now, onHover }: { events: CanvasEvent[]; palette: string[]; newest?: number; now: number; onHover: (event?: CanvasEvent) => void }) {
  return <ol className="canvas-activity">{events.length ? events.slice().reverse().slice(0, 20).map((event) => <ActivityRow key={event.revision} event={event} palette={palette} newest={newest} now={now} onHover={onHover} />) : <li className="muted">no activity yet</li>}</ol>;
}

export default function CanvasPage({ snapshot: initial, initialEvents = [], initialNow }: { snapshot: CanvasSnapshot; initialEvents?: CanvasEvent[]; initialNow?: string }) {
  const frame = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const stage = useRef<HTMLDivElement>(null);
  const toggle = useRef<HTMLButtonElement>(null);
  const close = useRef<HTMLButtonElement>(null);
  const mode = useCanvasMode();
  const live = useCanvasLive(initial, initialEvents, mode, canvas);
  const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
  const replay = useCanvasReplay({ mode, live, canvas, reduced });
  const [activity, setActivity] = useState(false);
  const [marker, setMarker] = useState<CanvasEvent>();
  const [newest, setNewest] = useState<number>();
  const [now, setNow] = useState(() => initialNow ? Date.parse(initialNow) : Date.now());
  const empty = isEmpty(live.snapshot);
  const mobileModal = window.innerWidth < 900;

  useEffect(() => {
    const saved = window.innerWidth >= 900 && localStorage.getItem("canvas-activity") === "open";
    if (saved) setActivity(true);
  }, []);
  useEffect(() => {
    if (window.innerWidth >= 900) localStorage.setItem("canvas-activity", activity ? "open" : "closed");
  }, [activity]);
  useEffect(() => {
    if (!live.events.length) return;
    const timer = window.setInterval(() => setNow(Date.now()), 10_000);
    return () => window.clearInterval(timer);
  }, [live.events.length]);
  useEffect(() => {
    if (!live.newest || replay.active) return;
    setMarker(live.newest);
    setNewest(live.newest.revision);
    const timer = window.setTimeout(() => { setMarker(undefined); setNewest(undefined); }, 2500);
    return () => window.clearTimeout(timer);
  }, [live.newest, replay.active]);
  useCanvasFrameSize(stage, frame);
  useActivityModal(activity, setActivity, mobileModal, toggle, close);
  useEffect(() => {
    const escape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      if (activity) setActivity(false);
      else if (replay.active) void replay.stop();
    };
    document.addEventListener("keydown", escape);
    return () => document.removeEventListener("keydown", escape);
  }, [activity, replay.active]);

  const currentRevision = replay.active ? replay.revision : live.snapshot.revision;
  const markerStyle = canvasMarkerStyle(marker);
  const rows = useMemo(() => <ActivityRows events={live.events} palette={live.snapshot.palette} newest={newest} now={now} onHover={(event) => { if (!replay.active) setMarker(event); }} />, [live.events, live.snapshot.palette, newest, now, replay.active]);
  const toggleActivity = () => setActivity((value) => !value);

  return <><main className="canvas-instrument" data-activity={activity ? "open" : "closed"} inert={mobileModal && activity ? true : undefined}>
    <h1 className="sr-only">canvas</h1>
    <div className="canvas-pill">
      <a className="canvas-home" href="/">neenerweener</a><span className="canvas-divider" />
      {!replay.active ? <><span className="canvas-live"><i /> live · rev {live.snapshot.revision.toLocaleString()}</span><span className="canvas-divider" /><button className="canvas-pill-action" type="button" onClick={() => !empty && void replay.start()} aria-disabled={empty || undefined}><span className="play-icon" />replay</button><a className="canvas-pill-action" href={empty ? undefined : "/api/canvas.png"} download="neenerweener-canvas.png" aria-label="download png" aria-disabled={empty || undefined}><span className="download-icon" />png</a></> : <div className="canvas-transport" aria-busy={!replay.history.length}>
        <button ref={replay.playButton} type="button" className="canvas-play" onClick={replay.togglePlaying} aria-label={replay.playing ? "pause replay" : "play replay"}><span className={replay.playing ? "pause-icon" : "play-icon"} /></button>
        <input type="range" min="0" max={replay.history.length} value={replay.position} onPointerDown={replay.beginSeek} onPointerUp={replay.endSeek} onBlur={replay.endSeek} onKeyDown={replay.keySeek} onChange={(event) => replay.seek(Number(event.currentTarget.value))} aria-label="replay position" aria-valuetext={`rev ${currentRevision.toLocaleString()} of ${replay.history.length.toLocaleString()}`} />
        <span className="canvas-position">rev {currentRevision.toLocaleString()} / {replay.history.length.toLocaleString()}</span>
        <button type="button" className="canvas-speed" onClick={replay.cycleSpeed} aria-label={`replay speed, ${replay.speed}×`}>{replay.speed}×</button><span className="canvas-divider" /><button type="button" className="canvas-stop" onClick={() => void replay.stop()}><span />stop replay</button>
      </div>}
      <span className="canvas-divider" /><button ref={toggle} className="canvas-pill-action" type="button" onClick={toggleActivity} aria-expanded={activity} aria-controls="canvas-activity" aria-label={activity ? "hide activity" : "show activity"}><span className="activity-icon" /> <span className="activity-label">activity</span></button>
    </div>
    <div ref={stage} className="canvas-stage"><div ref={frame} className="canvas-frame"><img src="/api/canvas.png" width="512" height="512" alt="" aria-hidden="true" decoding="async" /><canvas width="512" height="512" role="img" aria-label={replay.active ? `canvas at rev ${currentRevision.toLocaleString()} of ${replay.history.length.toLocaleString()}` : "current canvas"} /><span className={`canvas-marker${marker?.event_kind === "clear" ? " is-clear" : ""}`} hidden={!marker || replay.active} style={markerStyle} /></div>{empty && <span className="canvas-empty-label">no pixels yet</span>}<p className="canvas-status" role={live.status ? "status" : undefined} aria-live={live.status ? "polite" : undefined}><i />{live.status || `live · rev ${live.snapshot.revision.toLocaleString()}`}</p></div>
  </main>
    {activity && <button className="canvas-scrim" type="button" aria-label="close activity" onClick={() => setActivity(false)} />}
    <aside id="canvas-activity" className="canvas-panel" aria-labelledby="canvas-activity-title" aria-modal={mobileModal || undefined} hidden={!activity}><header><h2 id="canvas-activity-title">recent activity</h2><button ref={close} type="button" onClick={() => setActivity(false)} aria-label="close activity">×</button></header>{rows}</aside>
    <details className="canvas-nojs-activity"><summary>recent activity</summary>{rows}</details>
  </>;
}
