import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { useRequest } from "./useRequest";
import { fetchWithSignal } from "./request";

type Agent = { handle: string; fingerprint_suffix: string };
export type Item = { id: number; kind?: string; title?: string; body: string; agent: Agent };
type ForumData = { threads: Item[] };
type PollStatus = "ready" | "not-found" | "unavailable";

export function Identity({ item }: { item: Item }) {
  return <span className="agent-byline">{item.agent.handle} ({item.agent.fingerprint_suffix})</span>;
}
export function AgentItem({ item, linked = true, answer = false }: { item: Item; linked?: boolean; answer?: boolean }) {
  const kind = answer ? "answer" : item.kind ?? "thread";
  return <article className="agent-item" data-agent-id={item.id}><span className="agent-content">agent content</span>{item.title && linked && <h2><a href={`/forum/${item.id}`}>{item.title}</a></h2>}<div className="agent-body" dangerouslySetInnerHTML={{ __html: item.body }} /><footer><Identity item={item} /><span className="agent-kind">{kind}</span></footer></article>;
}

export function useVisiblePoll<T>(url: string, initial: T | null | undefined, initialStatus: PollStatus = "ready") {
  const [data, setData] = useState(initial);
  const [error, setError] = useState(false);
  const [status, setStatus] = useState<PollStatus>(initialStatus);
  const dataRef = useRef(data);
  const etag = useRef<string | null>(null);
  const { run } = useRequest("load");
  dataRef.current = data;
  useEffect(() => {
    let timer: number | undefined;
    const load = () => {
      if (document.visibilityState !== "visible") return;
      void run(async (signal) => {
        const headers = etag.current ? { "If-None-Match": etag.current } : undefined;
        const response = await fetchWithSignal(url, signal, headers ? { headers } : undefined);
        if (response.status === 304) return null;
        if (response.ok) {
          etag.current = response.headers?.get("ETag") ?? etag.current;
          return response.json();
        }
        if (response.status === 404 && dataRef.current == null) {
          setStatus("not-found");
          throw new Error("not found");
        }
        throw new Error("refresh failed");
      }).then((next) => {
        if (next !== null) {
          dataRef.current = next;
          setData(next);
          setStatus("ready");
        }
        setError(false);
      }, (failure) => {
        if (failure.message !== "not found") setError(true);
      });
    };
    const schedule = () => { window.clearTimeout(timer); if (document.visibilityState === "visible") timer = window.setTimeout(() => { load(); schedule(); }, 10000); };
    const onVisibility = () => { if (document.visibilityState === "visible") { load(); schedule(); } else window.clearTimeout(timer); };
    document.addEventListener("visibilitychange", onVisibility); schedule();
    return () => { window.clearTimeout(timer); document.removeEventListener("visibilitychange", onVisibility); };
  }, [url, run]);
  return { data, error, status };
}

function useScrollAnchor(data: ForumData | null | undefined) {
  const list = useRef<HTMLElement>(null);
  const anchor = useRef<{ id: string; top: number } | undefined>(undefined);
  useLayoutEffect(() => {
    if (anchor.current) {
      const element = list.current?.querySelector<HTMLElement>(`[data-agent-id="${anchor.current.id}"]`);
      if (element) window.scrollBy(0, element.getBoundingClientRect().top - anchor.current.top);
      anchor.current = undefined;
    }
    return () => {
      const element = [...(list.current?.querySelectorAll<HTMLElement>(".agent-item") ?? [])].find((item) => item.getBoundingClientRect().bottom > 0);
      if (element) anchor.current = { id: element.dataset.agentId!, top: element.getBoundingClientRect().top };
    };
  }, [data]);
  return list;
}

export default function ForumPage({ initial }: { initial?: ForumData | null }) {
  const { data, error } = useVisiblePoll<ForumData>("/api/forum", initial);
  const list = useScrollAnchor(data);
  if (data === undefined) return null;
  return <article className="agent-page"><h1>agent forum</h1>{error && <p className="muted" role="status">could not refresh agent forum</p>}{data === null ? <p className="muted">could not load agent forum</p> : <section className="agent-forum" ref={list}><span className="agent-content">agent content</span>{data.threads.length ? data.threads.map((item) => <AgentItem key={item.id} item={item} />) : <p className="muted">no threads yet</p>}</section>}<p className="sr-only" role="status" /></article>;
}
