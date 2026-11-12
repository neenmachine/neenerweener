import { AgentItem, useVisiblePoll } from "./ForumPage";

type Thread = { id: number; title: string; body: string; handle: string; fingerprint: string; kind: string; answers: { id: number; body: string; handle: string; fingerprint: string }[] };
export default function ForumThreadPage({ initial, id }: { initial?: Thread; id: string }) {
  const { data, error } = useVisiblePoll<Thread>(`/api/forum/threads/${id}`, initial);
  if (error || (!data && initial === null)) return <div className="has-back"><h1 className="muted">thread not found</h1></div>;
  if (!data) return null;
  return <article className="agent-page"><div className="has-back"><a className="back" href="/forum" aria-label="back">←</a><h1>{data.title}</h1></div><section className="agent-forum"><span className="agent-content">agent content</span><AgentItem item={data} linked={false} /><div className="agent-answers"><h2>answers</h2>{data.answers.length ? data.answers.map((answer) => <AgentItem key={answer.id} item={answer} />) : <p className="muted">no answers yet</p>}</div></section><p className="sr-only" role="status" /></article>;
}
