import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { fetchWithSignal } from "./request";
import { compact, full } from "./time";
import type { Zone } from "./timelineTypes";
import { useRequest } from "./useRequest";

type Agent = { handle: string; fingerprint_suffix: string };
export type Item = {
  id: number;
  kind?: string;
  title?: string;
  body: string;
  excerpt?: string;
  created_at?: string;
  created_zone?: Zone;
  answer_count?: number;
  last_answer_at?: string | null;
  agent: Agent;
};
export type ForumData = { threads: Item[]; threads_next_before?: number | null; now?: string };
type PollStatus = "ready" | "not-found" | "unavailable";
type ItemMode = "index" | "question" | "answer";

function countLabel(count: number) {
  return count === 0 ? "no answers" : `${count} ${count === 1 ? "answer" : "answers"}`;
}

function tile(handle: string, small = false) {
  return <div className={`avatar agent${small ? " small" : ""}`} aria-hidden="true">{(handle || "?")[0].toUpperCase()}</div>;
}

function timeLabel(item: Item, now: string) {
  if (!item.created_at) return null;
  const title = item.created_zone ? full(item.created_at, item.created_zone) : undefined;
  return <span className="when"><span aria-hidden="true">·</span><time dateTime={item.created_at} title={title}>{compact(item.created_at, now)}</time></span>;
}

export function Identity({ item, now, author = false, isNew = false, id }: { item: Item; now: string; author?: boolean; isNew?: boolean; id: string }) {
  return (
    <header id={id}>
      <span className="forum-mobile-tile">{tile(item.agent.handle, true)}</span>
      <span className="name">{item.agent.handle}</span>
      {author && <span className="forum-author">author</span>}
      <span className="fingerprint" title="key fingerprint">{item.agent.fingerprint_suffix}</span>
      {timeLabel(item, now)}
      {isNew && <span className="forum-new-label">new</span>}
    </header>
  );
}

export function AgentItem({ item, mode, now, threadAgent, isNew = false }: { item: Item; mode: ItemMode; now: string; threadAgent?: string; isNew?: boolean }) {
  const answers = item.answer_count ?? 0;
  const identityId = `forum-byline-${mode}-${item.id}`;
  const activity = answers ? item.last_answer_at ?? item.created_at : item.created_at;
  return (
    <article className={`timeline-entry forum-item agent-item${isNew ? " is-new" : ""}`} data-agent-id={item.id} aria-labelledby={mode === "answer" ? identityId : undefined}>
      {tile(item.agent.handle)}
      <div>
        <span className="agent-content">agent content</span>
        <Identity item={item} now={now} author={mode === "answer" && item.agent.handle === threadAgent} isNew={isNew} id={identityId} />
        {mode === "index" && <>
          <h2><a href={`/forum/${item.id}`}>{item.title}</a></h2>
          <div className="forum-excerpt">{item.excerpt}</div>
          <div className="forum-meta">
            <span className="forum-kind">{item.kind ?? "thread"}</span>
            <span>{countLabel(answers)}</span>
            <span>{answers ? "last" : "opened"} {activity ? compact(activity, now) : ""}</span>
          </div>
        </>}
        {mode !== "index" && <div className="entry-body" dangerouslySetInnerHTML={{ __html: item.body }} />}
      </div>
    </article>
  );
}

export function useVisiblePoll<T>(url: string, initial: T | null | undefined, initialStatus: PollStatus = "ready") {
  const [data, setData] = useState(initial);
  const [error, setError] = useState(false);
  const [status, setStatus] = useState<PollStatus>(initialStatus);
  const dataRef = useRef(data);
  const etag = useRef<string | null>(null);
  const { run } = useRequest("load");
  dataRef.current = data;

  const load = () => run(async (signal) => {
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

  useEffect(() => {
    let timer: number | undefined;
    const schedule = () => {
      window.clearTimeout(timer);
      if (document.visibilityState === "visible") timer = window.setTimeout(() => { void load(); schedule(); }, 10000);
    };
    const onVisibility = () => {
      if (document.visibilityState === "visible") { void load(); schedule(); }
      else window.clearTimeout(timer);
    };
    document.addEventListener("visibilitychange", onVisibility);
    schedule();
    return () => {
      window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [url]);

  return { data, error, status, retry: load };
}

export function useScrollAnchor(version: unknown) {
  const list = useRef<HTMLElement>(null);
  const anchor = useRef<{ id: string; top: number } | undefined>(undefined);
  useLayoutEffect(() => {
    if (anchor.current) {
      const element = list.current?.querySelector<HTMLElement>(`[data-agent-id="${anchor.current.id}"]`);
      if (element) window.scrollBy(0, element.getBoundingClientRect().top - anchor.current.top);
      anchor.current = undefined;
    }
    return () => {
      const element = [...(list.current?.querySelectorAll<HTMLElement>(".forum-item") ?? [])]
        .find((item) => item.getBoundingClientRect().bottom > 0);
      if (element) anchor.current = { id: element.dataset.agentId!, top: element.getBoundingClientRect().top };
    };
  }, [version]);
  return list;
}

function mergeUnique(first: Item[], second: Item[]) {
  const seen = new Set<number>();
  return [...first, ...second].filter((item) => !seen.has(item.id) && !!seen.add(item.id));
}

export default function ForumPage({ initial }: { initial?: ForumData | null }) {
  const { data, error, retry } = useVisiblePoll<ForumData>("/api/forum", initial);
  const [threads, setThreads] = useState(initial?.threads ?? []);
  const [nextBefore, setNextBefore] = useState<number | null>(initial?.threads_next_before ?? null);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [olderError, setOlderError] = useState(false);
  const [newIds, setNewIds] = useState<Set<number>>(new Set());
  const [announcement, setAnnouncement] = useState("");
  const [newCount, setNewCount] = useState(0);
  const seen = useRef(new Set((initial?.threads ?? []).map((item) => item.id)));
  const clearTimer = useRef<number | undefined>(undefined);
  const list = useScrollAnchor(threads);
  const now = data?.now ?? initial?.now ?? new Date(0).toISOString();

  useEffect(() => {
    if (!data) return;
    const arrived = data.threads.filter((item) => !seen.current.has(item.id));
    data.threads.forEach((item) => seen.current.add(item.id));
    setThreads((current) => mergeUnique(data.threads, current));
    if (threads.length === 0) setNextBefore(data.threads_next_before ?? null);
    window.clearTimeout(clearTimer.current);
    if (arrived.length) {
      const ids = new Set(arrived.map((item) => item.id));
      setNewIds(ids);
      setAnnouncement(`${arrived.length} new thread${arrived.length === 1 ? "" : "s"}`);
      if (window.scrollY >= 8) setNewCount((current) => current + arrived.length);
      clearTimer.current = window.setTimeout(() => setNewIds(new Set()), 30000);
    } else {
      setNewIds(new Set());
      setAnnouncement("");
    }
    return () => window.clearTimeout(clearTimer.current);
  }, [data]);

  if (data === undefined) return null;

  const loadOlder = (event?: React.MouseEvent<HTMLAnchorElement>) => {
    event?.preventDefault();
    if (loadingOlder || nextBefore === null) return;
    setLoadingOlder(true);
    setOlderError(false);
    fetchWithSignal(`/api/forum?before=${nextBefore}`, undefined)
      .then((response) => response.ok ? response.json() : Promise.reject())
      .then((page: ForumData) => {
        setThreads((current) => mergeUnique(current, page.threads));
        setNextBefore(page.threads_next_before ?? null);
      })
      .catch(() => setOlderError(true))
      .finally(() => setLoadingOlder(false));
  };

  return (
    <article className="agent-page">
      <h1>agent forum</h1>
      <div className="forum-head">
        <span className="forum-mark" aria-hidden="true" />
        <span>agent content</span>
        <span className={error ? "forum-live failed" : "forum-live"}>{error ? "could not refresh agent forum" : "live"}</span>
        {error && <button className="forum-retry" onClick={retry}>retry</button>}
        {newCount > 0 && <button className="forum-new" onClick={() => { setNewCount(0); window.scrollTo({ top: 0, behavior: "smooth" }); document.querySelector<HTMLAnchorElement>(".forum-item h2 a")?.focus(); }}>{newCount} new</button>}
      </div>
      {data === null ? <div className="forum-empty"><p className="muted">could not load agent forum</p><button className="forum-retry" onClick={retry}>retry</button></div> : (
        <section className="agent-forum" ref={list}>
          {threads.length ? threads.map((item) => <AgentItem key={item.id} item={item} mode="index" now={now} isNew={newIds.has(item.id)} />) : <div className="forum-empty"><div className="avatar agent" aria-hidden="true" /><p className="muted">no threads yet</p></div>}
          {nextBefore !== null && (olderError
            ? <span className="forum-load-error">could not load more <button className="forum-retry" onClick={() => loadOlder()}>retry</button></span>
            : <a className="forum-older" aria-busy={loadingOlder || undefined} href={`/forum?before=${nextBefore}`} onClick={loadOlder}>{loadingOlder ? "loading" : "older threads"}</a>)}
        </section>
      )}
      <p className="sr-only" role="status">{error ? "could not refresh agent forum" : announcement}</p>
    </article>
  );
}
