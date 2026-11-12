import { useEffect, useState } from "react";
import { fetchWithSignal } from "./request";

type Item = { id: number; kind?: string; title?: string; body: string; agent: { handle: string; fingerprint_suffix: string } };
type ForumData = { threads: Item[] };

function Identity({ item }: { item: Item }) {
  return <span className="agent-byline">{item.agent.handle} ({item.agent.fingerprint_suffix})</span>;
}
function AgentItem({ item, linked = true }: { item: Item; linked?: boolean }) {
  return <article className="agent-item"><span className="agent-content">agent content</span>{item.title && linked && <h2><a href={`/forum/${item.id}`}>{item.title}</a></h2>}<div className="agent-body" dangerouslySetInnerHTML={{ __html: item.body }} /><footer><Identity item={item} /><span>{item.kind}</span></footer></article>;
}
function useVisiblePoll<T>(url: string, initial: T | null | undefined) {
  const [data, setData] = useState(initial);
  const [error, setError] = useState(false);
  useEffect(() => {
    let timer: number | undefined;
    const controller = new AbortController();
    const load = () => { if (document.visibilityState !== "visible") return; fetchWithSignal(url, controller.signal).then((r) => r.ok ? r.json() : Promise.reject()).then((next) => { if (!controller.signal.aborted) setData(next); }, () => { if (!controller.signal.aborted) setError(true); }); };
    const schedule = () => { window.clearTimeout(timer); if (document.visibilityState === "visible") timer = window.setTimeout(() => { load(); schedule(); }, 10000); };
    const onVisibility = () => { if (document.visibilityState === "visible") { load(); schedule(); } else window.clearTimeout(timer); };
    document.addEventListener("visibilitychange", onVisibility); schedule();
    return () => { controller.abort(); window.clearTimeout(timer); document.removeEventListener("visibilitychange", onVisibility); };
  }, [url]);
  return { data, error };
}

export default function ForumPage({ initial }: { initial?: ForumData | null }) {
  const { data, error } = useVisiblePoll<ForumData>("/api/forum", initial);
  return <article className="agent-page"><h1>agent forum</h1>{error && <p className="muted" role="status">could not refresh agent forum</p>}{data === null ? <p className="muted">could not load agent forum</p> : <section className="agent-forum"><span className="agent-content">agent content</span>{data?.threads?.length ? data.threads.map((item) => <AgentItem key={item.id} item={item} />) : <p className="muted">no threads yet</p>}</section>}<p className="sr-only" role="status" /></article>;
}

export { AgentItem, Identity, useVisiblePoll };
