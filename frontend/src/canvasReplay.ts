import { useEffect, useRef, useState, type RefObject } from "react";
import { fetchWithSignal } from "./request";
import { applyCanvasEvents, drawCanvas, type CanvasEvent, type CanvasMode, type CanvasSnapshot, type EventResponse } from "./canvasLive";

export function replayPositionToRevision(history: CanvasEvent[], position: number): number {
  return position > 0 ? history[Math.min(position, history.length) - 1]?.revision ?? 0 : 0;
}
export function replayStep(position: number, delta: number, length: number): number {
  return Math.max(0, Math.min(length, position + delta));
}
export function replayDelay(speed: number): number { return 35 / speed; }

export async function loadCanvasHistory(fetchPage: (after: number) => Promise<EventResponse>, signal: AbortSignal): Promise<CanvasEvent[]> {
  let cursor = 0;
  const history: CanvasEvent[] = [];
  while (!signal.aborted) {
    const result = await fetchPage(cursor);
    if (signal.aborted) return history;
    if (result.reset) return history;
    history.push(...result.events);
    if (!result.events.length) break;
    cursor = result.events.at(-1)!.revision;
    if (cursor >= result.revision) break;
  }
  return history;
}

export function useCanvasMode(): CanvasMode {
  return useRef<"live" | "replay">("live");
}

export function useCanvasReplay({ mode, live, canvas, reduced }: {
  mode: CanvasMode;
  live: { snapshot: CanvasSnapshot; restore(controller: AbortController): Promise<boolean>; reportStatus(value: string): void };
  canvas: RefObject<HTMLCanvasElement | null>;
  reduced: boolean;
}) {
  const [active, setActive] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [history, setHistory] = useState<CanvasEvent[]>([]);
  const [position, setPosition] = useState(0);
  const [speed, setSpeed] = useState(1);
  const replayController = useRef<AbortController | undefined>(undefined);
  const restoreController = useRef<AbortController | undefined>(undefined);
  const playButton = useRef<HTMLButtonElement>(null);
  const wasPlaying = useRef(false);

  const drawAt = (at: number, list = history) => {
    const pixels = applyCanvasEvents(new Uint8Array(live.snapshot.width * live.snapshot.height).fill(255), list.slice(0, at));
    if (canvas.current) drawCanvas(canvas.current, live.snapshot, pixels);
  };

  const start = async () => {
    mode.current = "replay";
    setActive(true);
    setPlaying(!reduced);
    setPosition(0);
    setHistory([]);
    drawAt(0, []);
    const controller = new AbortController();
    replayController.current = controller;
    try {
      const loaded = await loadCanvasHistory(async (after) => {
        const response = await fetchWithSignal(`/api/canvas/history?after=${after}&limit=500`, controller.signal);
        if (!response.ok) throw new Error("history failed");
        const result = await response.json() as EventResponse;
        if (controller.signal.aborted || mode.current !== "replay") return { reset: false, revision: after, events: [] };
        return result;
      }, controller.signal);
      if (controller.signal.aborted || mode.current !== "replay") return;
      setHistory(loaded);
      if (loaded.length && !reduced) setPosition(1);
      requestAnimationFrame(() => playButton.current?.focus());
    } catch (error) {
      if ((error as Error).name !== "AbortError") { setPlaying(false); live.reportStatus("replay failed; retrying"); }
    }
  };

  const stop = async () => {
    replayController.current?.abort();
    mode.current = "live";
    setPlaying(false);
    setActive(false);
    const controller = new AbortController();
    restoreController.current?.abort();
    restoreController.current = controller;
    await live.restore(controller);
    if (restoreController.current === controller) restoreController.current = undefined;
  };

  useEffect(() => () => { replayController.current?.abort(); restoreController.current?.abort(); }, []);
  useEffect(() => {
    if (!active || !playing || !history.length) return;
    const timer = window.setTimeout(() => setPosition((value) => {
      if (value >= history.length) { setPlaying(false); return value; }
      return value + 1;
    }), replayDelay(speed));
    return () => window.clearTimeout(timer);
  }, [active, playing, position, speed, history.length]);
  useEffect(() => { if (active) drawAt(position); }, [active, position, history]);

  const seek = (next: number) => setPosition(replayStep(next, 0, history.length));
  const keySeek = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (!event.shiftKey || !["ArrowLeft", "ArrowRight"].includes(event.key)) return;
    event.preventDefault();
    const amount = Math.max(1, Math.round(history.length * 0.1));
    seek(position + (event.key === "ArrowRight" ? amount : -amount));
  };
  const beginSeek = () => { wasPlaying.current = playing; setPlaying(false); };
  const endSeek = () => { if (wasPlaying.current && position < history.length) setPlaying(true); wasPlaying.current = false; };

  return {
    active, playing, history, position, speed, playButton,
    revision: replayPositionToRevision(history, position),
    start, stop, seek, keySeek, beginSeek, endSeek,
    togglePlaying: () => setPlaying((value) => !value),
    cycleSpeed: () => setSpeed((value) => value === 1 ? 4 : value === 4 ? 16 : 1),
  };
}
