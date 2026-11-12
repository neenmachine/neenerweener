import { useEffect, useMemo, useRef, useState } from "react";
import { AgentItem, useVisiblePoll, type Item } from "./ForumPage";
import { fetchWithSignal } from "./request";

type Thread = Item & { title: string; kind: string; answers: Item[]; answers_next_after: number | null };
export default function ForumThreadPage({ initial, id, after, status: initialStatus = "ready" }: { initial?: Thread | null; id: string; after?: string; status?: "ready" | "not-found" | "unavailable" }) {
  const pollUrl = after ? `/api/forum/threads/${id}?after=${after}` : `/api/forum/threads/${id}`;
  const { data: base, error, status } = useVisiblePoll<Thread>(pollUrl, initial, initialStatus);
  const [older, setOlder] = useState<Item[]>([]);
  const [nextAfter, setNextAfter] = useState<number | null>(initial?.answers_next_after ?? null);
  const [loading, setLoading] = useState(false);
  const life = useRef<AbortController | undefined>(undefined);
  useEffect(() => {
    if (base && older.length === 0) setNextAfter(base.answers_next_after);
  }, [base, older.length]);
  useEffect(() => {
    const controller = new AbortController();
    life.current = controller;
    return () => controller.abort();
  }, []);
  const answers = useMemo(() => {
    if (!base) return [];
    const seen = new Set<number>();
    return [...base.answers, ...older].filter((answer) => !seen.has(answer.id) && !!seen.add(answer.id));
  }, [base, older]);
  const loadMore = (event: React.MouseEvent<HTMLAnchorElement>) => {
    event.preventDefault();
    if (loading || nextAfter === null) return;
    setLoading(true);
    fetchWithSignal(`/api/forum/threads/${id}?after=${nextAfter}`, life.current?.signal).then((response) => response.ok ? response.json() : Promise.reject()).then((page: Thread) => {
      setOlder((current) => [...current, ...page.answers]);
      setNextAfter(page.answers_next_after);
    }).finally(() => setLoading(false));
  };
  if (!base) {
    const message = status === "unavailable" ? "could not load agent forum" : "thread not found";
    return <article className="agent-page"><div className="has-back"><a className="back" href="/forum" aria-label="back">←</a><h1 className="muted">{message}</h1></div><p className="sr-only" role="status" /></article>;
  }
  return <article className="agent-page"><div className="has-back"><a className="back" href="/forum" aria-label="back">←</a><h1>{base.title}</h1></div>{error && <p className="muted" role="status">could not refresh agent forum</p>}<section className="agent-forum"><span className="agent-content">agent content</span><AgentItem item={base} linked={false} /><div className="agent-answers"><h2>answers</h2>{answers.length ? answers.map((answer) => <AgentItem key={answer.id} item={answer} answer />) : <p className="muted">no answers yet</p>}{nextAfter !== null && <a className="answers-more" href={`/forum/${id}?after=${nextAfter}`} onClick={loadMore}>{loading ? "loading" : "more answers"}</a>}</div></section><p className="sr-only" role="status" /></article>;
}
