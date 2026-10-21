import { useEffect, useRef, useState } from "react";

type Summary = { slug: string; date: string | null; title: string; excerpt: string | null; draft: boolean };
type Post = Summary & { html: string };

// shortcut: plain links and pathname matching, no router. Add react-router when there are more pages.
const slug = location.pathname.match(/^\/posts\/([a-z0-9-]+)\/?$/)?.[1];

export default function App() {
  return (
    <main>
      {slug ? (
        <>
          <a className="back" href="/">
            ← back
          </a>
          <PostPage slug={slug} />
        </>
      ) : (
        <>
          <header>
            <a href="/">neenerweener</a>
          </header>
          <PostList />
        </>
      )}
    </main>
  );
}

function useApi<T>(url: string) {
  const [data, setData] = useState<T>();
  const [error, setError] = useState(false);
  useEffect(() => {
    fetch(url)
      .then((r) => (r.ok ? r.json() : Promise.reject()))
      .then(setData, () => setError(true));
  }, [url]);
  return { data, error };
}

function PostList() {
  const { data, error } = useApi<Summary[]>("/api/posts");
  if (error) return <p className="muted">could not load posts</p>;
  if (!data) return null;
  if (!data.length) return <p className="muted">no posts yet</p>;
  return (
    <ul className="posts">
      {data.map((p) => (
        <li key={p.slug}>
          {p.date && <time>{p.date}</time>}
          <div className="post-info">
            <a href={`/posts/${p.slug}`}>{p.title}</a>
            {p.excerpt && <div className="excerpt muted">{p.excerpt}</div>}
          </div>
          {p.draft && <span className="draft muted">draft</span>}
        </li>
      ))}
    </ul>
  );
}

function PostPage({ slug }: { slug: string }) {
  const { data, error } = useApi<Post>(`/api/posts/${slug}`);
  const body = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!data) return;
    document.title = data.title;
    const diagrams = body.current?.querySelectorAll<HTMLElement>("pre.mermaid");
    if (diagrams?.length) {
      // Mermaid is large, so only posts with diagrams load it.
      import("mermaid").then(({ default: mermaid }) => {
        mermaid.initialize({
          startOnLoad: false,
          theme: "neutral",
          fontFamily: "JetBrains Mono Variable, monospace",
        });
        mermaid.run({ nodes: diagrams });
      });
    }
    const math = body.current?.querySelectorAll<HTMLElement>(".math");
    if (math?.length) {
      // KaTeX is large, so only posts with math load it and its CSS.
      Promise.all([import("katex"), import("katex/dist/katex.min.css")]).then(
        ([{ default: katex }]) => {
          math.forEach((el) =>
            katex.render(el.textContent ?? "", el, {
              displayMode: el.classList.contains("block"),
              throwOnError: false,
            }),
          );
        },
      );
    }
  }, [data]);
  if (error) return <p className="muted">post not found</p>;
  if (!data) return null;
  return (
    <article>
      <h1>{data.title} {data.draft && <span className="draft muted">draft</span>}</h1>
      {data.date && <time>{data.date}</time>}
      {/* Posts are Henry's own files, rendered server side. */}
      <div ref={body} dangerouslySetInnerHTML={{ __html: data.html }} />
    </article>
  );
}
