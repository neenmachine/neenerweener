import { type ReactNode, useEffect, useRef, useState } from "react";

type Summary = { slug: string; date: string | null; title: string; excerpt: string | null; draft: boolean };
type Post = Summary & { html: string };
type Project = { name: string; files: string[]; readme_html: string | null };
type CodeFile = { path: string; html?: string; binary?: true };

// shortcut: plain links and pathname matching, no router. Add react-router when there are more pages.
const slug = location.pathname.match(/^\/posts\/([a-z0-9-]+)\/?$/)?.[1];
// /code, /code/<project>, /code/<project>/<path>; the path stays URL-encoded.
const code = location.pathname.match(/^\/code(?:\/([a-z0-9-]+)(?:\/(.+?))?)?\/?$/);
const IMAGE = /\.(png|jpe?g|webp|gif|svg)$/i;

export default function App() {
  return <main>{page()}</main>;
}

function page() {
  if (slug) return <PostPage slug={slug} />;
  if (code?.[2]) return <CodeFilePage project={code[1]} path={code[2]} />;
  if (code?.[1]) return <ProjectPage name={code[1]} />;
  return (
    <>
      <header>
        <a href="/">neenerweener</a>
        <a className="nav" href="/code">
          code
        </a>
      </header>
      {code ? <ProjectList /> : <PostList />}
    </>
  );
}

function Back({ href }: { href: string }) {
  return (
    <a className="back" href={href} aria-label="back">
      ←
    </a>
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
  const back = <Back href="/" />;
  if (error)
    return (
      <p className="muted has-back">
        {back}post not found
      </p>
    );
  if (!data) return null;
  return (
    <article>
      <h1 className="has-back">
        {back}
        {data.title} {data.draft && <span className="draft muted">draft</span>}
      </h1>
      {data.date && <time>{data.date}</time>}
      {/* Posts are Henry's own files, rendered server side. */}
      <div ref={body} dangerouslySetInnerHTML={{ __html: data.html }} />
    </article>
  );
}

function ProjectList() {
  const { data, error } = useApi<string[]>("/api/code");
  if (error) return <p className="muted">could not load projects</p>;
  if (!data) return null;
  if (!data.length) return <p className="muted">no projects yet</p>;
  return (
    <ul className="posts">
      {data.map((name) => (
        <li key={name}>
          <a href={`/code/${name}`}>{name}</a>
        </li>
      ))}
    </ul>
  );
}

function ProjectPage({ name }: { name: string }) {
  const { data, error } = useApi<Project>(`/api/code/${name}`);
  useEffect(() => {
    document.title = name;
  }, [name]);
  const back = <Back href="/code" />;
  if (error)
    return (
      <p className="muted has-back">
        {back}project not found
      </p>
    );
  if (!data) return null;
  return (
    <article>
      <h1 className="has-back">
        {back}
        {data.name}
      </h1>
      <FileTree project={data.name} files={data.files} />
      {/* The README is Henry's own file, rendered server side. */}
      {data.readme_html && <div dangerouslySetInnerHTML={{ __html: data.readme_html }} />}
    </article>
  );
}

type Tree = Map<string, Tree | null>;

// Indented by depth, directories before files.
function FileTree({ project, files }: { project: string; files: string[] }) {
  const root: Tree = new Map();
  for (const file of files) {
    const parts = file.split("/");
    let node = root;
    for (const dir of parts.slice(0, -1)) {
      if (!node.has(dir)) node.set(dir, new Map());
      node = node.get(dir)!;
    }
    node.set(parts[parts.length - 1], null);
  }
  const rows: ReactNode[] = [];
  const walk = (node: Tree, prefix: string, depth: number) => {
    const entries = [...node].sort(([, a], [, b]) => Number(!a) - Number(!b));
    for (const [name, child] of entries) {
      const path = prefix + name;
      rows.push(
        <li key={path} style={{ paddingLeft: `${depth * 2}ch` }}>
          {child ? (
            <span className="muted">{name}/</span>
          ) : (
            <a href={`/code/${project}/${path.split("/").map(encodeURIComponent).join("/")}`}>{name}</a>
          )}
        </li>,
      );
      if (child) walk(child, path + "/", depth + 1);
    }
  };
  walk(root, "", 0);
  return <ul className="tree">{rows}</ul>;
}

function CodeFilePage({ project, path }: { project: string; path: string }) {
  const { data, error } = useApi<CodeFile>(`/api/code/${project}/file/${path}`);
  const title = decodeURIComponent(path);
  useEffect(() => {
    document.title = title;
  }, [title]);
  const back = <Back href={`/code/${project}`} />;
  if (error)
    return (
      <p className="muted has-back">
        {back}file not found
      </p>
    );
  if (!data) return null;
  return (
    <article>
      <h1 className="has-back code-path">
        {back}
        {title}
      </h1>
      {IMAGE.test(title) ? (
        <img className="code-image" src={`/code-raw/${project}/${path}`} alt={title} />
      ) : data.html === undefined ? (
        <p className="muted">binary file</p>
      ) : (
        <pre className="code-file">
          {/* Highlighted server side by Pygments, which escapes the source. */}
          <code dangerouslySetInnerHTML={{ __html: data.html }} />
        </pre>
      )}
    </article>
  );
}
