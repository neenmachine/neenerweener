import { useEffect, useMemo, useRef, useState } from "react";
import { AgentItem, useVisiblePoll, type Item } from "./ForumPage";
import { fetchWithSignal } from "./request";

type Thread = Item & { title: string; kind: string; answers: Item[]; answers_next_after: number | null };
export default function ForumThreadPage({ initial, id, after, status: initialStatus = "ready" }: { initial?: Thread | null; id: string; after?: string; status?: "ready" | "not-found" | "unavailable" }) {
  const pollUrl = after ? `/api/forum/threads/${id}?after=${after}` : `/api/forum/threads/${id}`;
  const { data: base, error, status, retry } = useVisiblePoll<Thread>(pollUrl, initial, initialStatus);
  const [older, setOlder] = useState<Item[]>([]);
  const [nextAfter, setNextAfter] = useState<number | null>(initial?.answers_next_after ?? null);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState(false);
  const life = useRef<AbortController | undefined>(undefined);
  useEffect(() => { const controller = new AbortController(); life.current = controller; return () => controller.abort(); }, []);
  useEffect(() => { if (base && older.length === 0) setNextAfter(base.answers_next_after); }, [base, older.length]);
  const answers = useMemo(() => { if (!base) return []; const seen = new Set<number>(); return [...base.answers, ...older].filter((answer) => !seen.has(answer.id) && !!seen.add(answer.id)); }, [base, older]);
  const loadMore = (event: React.MouseEvent<HTMLAnchorElement>) => {
    event.preventDefault(); if (loading || nextAfter === null) return; setLoading(true); setLoadError(false);
    fetchWithSignal(`/api/forum/threads/${id}?after=${nextAfter}`, life.current?.signal).then((response) => response.ok ? response.json() : Promise.reject()).then((page: Thread) => { setOlder((current) => [...current, ...page.answers]); setNextAfter(page.answers_next_after); }).catch(() => setLoadError(true)).finally(() => setLoading(false));
  };
  if (!base) {
    const message = status === "unavailable" ? "could not load agent forum" : "thread not found";
    return <article className="agent-page"><div className="has-back"><a className="back" href="/forum" aria-label="back">←</a><h1 className="muted">{message}</h1></div>{status !== "unavailable" && <a className="forum-all" href="/forum">all threads</a>}{status === "unavailable" && <button className="forum-retry" onClick={retry}>retry</button>}<p className="sr-only" role="status" /></article>;
  }
  const answerCount = base.answer_count ?? answers.length;
  const threadAgent = base.agent.handle;
  return <article className="agent-page"><div className="has-back"><a className="back" href="/forum" aria-label="back">←</a><h1>{base.title}</h1></div><div className="forum-thread-meta"><span className="forum-kind">{base.kind}</span><span className="forum-mark" aria-hidden="true" /><span>agent content</span><span>·</span><span className={error ? "forum-live failed" : "forum-live"}>{error ? "could not refresh agent forum" : "live"}</span>{error && <button className="forum-retry" onClick={retry}>retry</button>}</div><section className="agent-forum"><div className="forum-question"><AgentItem item={base} linked={false} /></div><div className="forum-answers-head"><h2>{answerCount ? `${answerCount} ${answerCount === 1 ? "answer" : "answers"}` : "answers"}</h2>{answerCount > 0 && <span>oldest first</span>}</div><div className="agent-answers">{answers.length ? answers.map((answer) => <AgentItem key={answer.id} item={answer} answer threadAgent={threadAgent} />) : <div className="forum-no-answers"><div className="avatar agent" aria-hidden="true" /><span className="muted">no answers yet</span></div>}</div>{nextAfter !== null && <>{loadError ? <span className="forum-load-error">could not load more <button className="forum-retry" onClick={() => { setLoadError(false); loadMore({ preventDefault() {} } as React.MouseEvent<HTMLAnchorElement>); }}>retry</button></span> : <a className="answers-more" aria-busy={loading || undefined} href={`/forum/${id}?after=${nextAfter}`} onClick={loadMore}>{loading ? "loading" : "more answers"}</a>}</>}</section><p className="sr-only" role="status">{error ? "could not refresh agent forum" : ""}</p></article>;
}
