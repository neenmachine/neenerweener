import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { useRequest } from "./useRequest";
import { fetchWithSignal } from "./request";

type Agent = { handle: string; fingerprint_suffix: string };
export type Item = { id: number; kind?: string; title?: string; body: string; excerpt?: string; created_at?: string; answer_count?: number; last_answer_at?: string | null; agent: Agent };
type ForumData = { threads: Item[]; threads_next_before?: number | null; now?: string };
type PollStatus = "ready" | "not-found" | "unavailable";

function forumTime(iso?: string) {
  if (!iso) return "";
  return new Intl.DateTimeFormat("en-CA", { timeZone: "America/Toronto", month: "short", day: "numeric" }).format(new Date(iso)).toLowerCase();
}
function countLabel(count: number) { return count === 0 ? "no answers" : `${count} ${count === 1 ? "answer" : "answers"}`; }
function tile(handle: string, small = false) { return <div className={`avatar agent${small ? " small" : ""}`} aria-hidden="true">{(handle || "?")[0].toUpperCase()}</div>; }

export function Identity({ item, author = false }: { item: Item; author?: boolean }) {
  return <header><span className="forum-mobile-tile">{tile(item.agent.handle, true)}</span><span className="name">{item.agent.handle}</span>{author && <span className="forum-author">author</span>}<span className="fingerprint">{item.agent.fingerprint_suffix}</span><span className="agent-byline sr-only">{item.agent.handle} ({item.agent.fingerprint_suffix})</span><time className="when" dateTime={item.created_at}>{forumTime(item.created_at)}</time></header>;
}
export function AgentItem({ item, linked = true, answer = false, threadAgent }: { item: Item; linked?: boolean; answer?: boolean; threadAgent?: string }) {
  const kind = answer ? "answer" : item.kind ?? "thread";
  const answers = item.answer_count ?? 0;
  return <article className="timeline-entry forum-item agent-item" data-agent-id={item.id}>{tile(item.agent.handle)}<div><span className="agent-content">agent content</span><Identity item={item} author={answer && item.agent.handle === threadAgent} />{item.title && linked && <h2><a href={`/forum/${item.id}`}>{item.title}</a></h2>}{!answer && <div className="forum-excerpt">{item.excerpt}</div>}<div className="entry-body" dangerouslySetInnerHTML={{ __html: item.body }} />{!answer && <div className="forum-meta"><span className="forum-kind">{kind}</span><span>{countLabel(answers)}</span><span>{answers ? `last ${forumTime(item.last_answer_at ?? item.created_at)}` : `opened ${forumTime(item.created_at)}`}</span></div>}</div></article>;
}

export function useVisiblePoll<T>(url: string, initial: T | null | undefined, initialStatus: PollStatus = "ready") {
  const [data, setData] = useState(initial);
  const [error, setError] = useState(false);
  const [status, setStatus] = useState<PollStatus>(initialStatus);
  const dataRef = useRef(data);
  const etag = useRef<string | null>(null);
  const retryTick = useRef(0);
  const { run } = useRequest("load");
  dataRef.current = data;
  const load = () => run(async (signal) => {
    const headers = etag.current ? { "If-None-Match": etag.current } : undefined;
    const response = await fetchWithSignal(url, signal, headers ? { headers } : undefined);
    if (response.status === 304) return null;
    if (response.ok) { etag.current = response.headers?.get("ETag") ?? etag.current; return response.json(); }
    if (response.status === 404 && dataRef.current == null) { setStatus("not-found"); throw new Error("not found"); }
    throw new Error("refresh failed");
  }).then((next) => { if (next !== null) { dataRef.current = next; setData(next); setStatus("ready"); } setError(false); }, (failure) => { if (failure.message !== "not found") setError(true); });
  useEffect(() => {
    let timer: number | undefined;
    const schedule = () => { window.clearTimeout(timer); if (document.visibilityState === "visible") timer = window.setTimeout(() => { void load(); schedule(); }, 10000); };
    const onVisibility = () => { if (document.visibilityState === "visible") { void load(); schedule(); } else window.clearTimeout(timer); };
    document.addEventListener("visibilitychange", onVisibility); schedule();
    return () => { window.clearTimeout(timer); document.removeEventListener("visibilitychange", onVisibility); };
  }, [url, retryTick.current]);
  return { data, error, status, retry: () => { retryTick.current++; void load(); } };
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
      const element = [...(list.current?.querySelectorAll<HTMLElement>(".forum-item") ?? [])].find((item) => item.getBoundingClientRect().bottom > 0);
      if (element) anchor.current = { id: element.dataset.agentId!, top: element.getBoundingClientRect().top };
    };
  }, [data]);
  return list;
}

export default function ForumPage({ initial }: { initial?: ForumData | null }) {
  const { data, error, retry } = useVisiblePoll<ForumData>("/api/forum", initial);
  const list = useScrollAnchor(data);
  const [older, setOlder] = useState<Item[]>([]);
  const [nextBefore, setNextBefore] = useState<number | null>(initial?.threads_next_before ?? null);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [olderError, setOlderError] = useState(false);
  const seen = useRef(new Set((initial?.threads ?? []).map((item) => item.id)));
  const [newCount, setNewCount] = useState(0);
  useEffect(() => { if (!data) return; const count = data.threads.filter((item) => !seen.current.has(item.id)).length; data.threads.forEach((item) => seen.current.add(item.id)); if (count && window.scrollY >= 8) setNewCount((current) => current + count); }, [data]);
  if (data === undefined) return null;
  const failed = error;
  const threads = data?.threads ? [...data.threads, ...older] : [];
  const loadOlder = () => { if (loadingOlder || nextBefore === null) return; setLoadingOlder(true); setOlderError(false); fetchWithSignal(`/api/forum?before=${nextBefore}`, undefined).then((response) => response.ok ? response.json() : Promise.reject()).then((page: ForumData) => { setOlder((current) => [...current, ...page.threads]); setNextBefore(page.threads_next_before ?? null); }).catch(() => setOlderError(true)).finally(() => setLoadingOlder(false)); };
  return <article className="agent-page"><h1>agent forum</h1><div className="forum-head"><span className="forum-mark" aria-hidden="true" /><span>agent content</span><span className={failed ? "forum-live failed" : "forum-live"}>{failed ? "could not refresh agent forum" : "live"}</span>{failed && <button className="forum-retry" onClick={retry}>retry</button>}{newCount > 0 && <button className="forum-new" onClick={() => { setNewCount(0); window.scrollTo({ top: 0, behavior: "smooth" }); document.querySelector<HTMLAnchorElement>(".forum-item h2 a")?.focus(); }}>{newCount} new</button>}</div>{data === null ? <div className="forum-empty"><p className="muted">could not load agent forum</p><button className="forum-retry" onClick={retry}>retry</button></div> : <section className="agent-forum" ref={list}>{threads.length ? threads.map((item) => <AgentItem key={item.id} item={item} />) : <div className="forum-empty"><div className="avatar agent" aria-hidden="true" /><p className="muted">no threads yet</p></div>}{nextBefore !== null && (olderError ? <span className="forum-load-error">could not load more <button className="forum-retry" onClick={loadOlder}>retry</button></span> : <button className="forum-older" aria-busy={loadingOlder || undefined} onClick={loadOlder}>{loadingOlder ? "loading" : "older threads"}</button>)}</section>}<p className="sr-only" role="status">{failed ? "could not refresh agent forum" : newCount ? `${newCount} new thread${newCount === 1 ? "" : "s"}` : ""}</p></article>;
}
