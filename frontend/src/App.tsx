import { type ReactNode, useEffect, useRef, useState } from "react";

type Summary = { slug: string; date: string | null; title: string; excerpt: string | null; draft: boolean };
type Post = Summary & { html: string };
type Project = { name: string; files: string[]; readme_html: string | null };
type CodeFile = { path: string; html?: string; binary?: true };

// shortcut: plain links and pathname matching, no router. Add react-router when there are more pages.
const slug = location.pathname.match(/^\/posts\/([a-z0-9-]+)\/?$/)?.[1];
// /code, /code/<project>, /code/<project>/<path>, where the path is a folder or a file.
const code = location.pathname.match(/^\/code(?:\/([a-z0-9-]+)(?:\/(.+?))?)?\/?$/);
const IMAGE = /\.(png|jpe?g|webp|gif|svg)$/i;

export default function App() {
  // Project pages are wider, like a repo browser, so long code lines fit.
  return <main className={code?.[1] ? "wide" : undefined}>{page()}</main>;
}

function page() {
  if (slug) return <PostPage slug={slug} />;
  if (code?.[1]) return <ProjectPage name={code[1]} path={code[2] ? decodeURIComponent(code[2]) : ""} />;
  return (
    <>
      <header>
        <a href="/">neenerweener</a>
        <a className="nav" href="/code" aria-current={code ? "page" : undefined}>
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
  useEffect(() => {
    document.title = "code";
  }, []);
  if (error) return <p className="muted">could not load projects</p>;
  if (!data) return null;
  if (!data.length) return <p className="muted">no projects yet</p>;
  return (
    <ul className="box listing">
      {data.map((name) => (
        <li key={name}>
          <a href={`/code/${name}`}>
            <Icon dir />
            {name}
          </a>
        </li>
      ))}
    </ul>
  );
}

const href = (project: string, path: string) =>
  `/code/${project}${path && "/" + path.split("/").map(encodeURIComponent).join("/")}`;

// code / project / dir / file, every segment but the last a link.
function Crumbs({ project, path }: { project: string; path: string }) {
  const parts = path ? path.split("/") : [];
  return (
    <h1 className="crumbs">
      <a href="/code">code</a>
      <span className="sep">/</span>
      {parts.length ? <a href={href(project, "")}>{project}</a> : project}
      {parts.map((part, i) => (
        <span key={i}>
          <span className="sep">/</span>
          {i < parts.length - 1 ? <a href={href(project, parts.slice(0, i + 1).join("/"))}>{part}</a> : part}
        </span>
      ))}
    </h1>
  );
}

// The API lists every file in the project; folders are derived from those paths.
function ProjectPage({ name, path }: { name: string; path: string }) {
  const { data, error } = useApi<Project>(`/api/code/${name}`);
  useEffect(() => {
    document.title = path || name;
  }, [name, path]);
  const crumbs = <Crumbs project={name} path={path} />;
  if (error)
    return (
      <>
        {crumbs}
        <p className="muted">project not found</p>
      </>
    );
  if (!data) return crumbs;
  if (data.files.includes(path)) return <FileView project={name} path={path} crumbs={crumbs} />;
  const prefix = path && path + "/";
  const dirs = new Set<string>();
  const files: string[] = [];
  for (const file of data.files) {
    if (!file.startsWith(prefix)) continue;
    const [first, ...rest] = file.slice(prefix.length).split("/");
    if (rest.length) dirs.add(first);
    else files.push(first);
  }
  if (!dirs.size && !files.length)
    return (
      <>
        {crumbs}
        <p className="muted">file not found</p>
      </>
    );
  return (
    <>
      {crumbs}
      <ul className="box listing">
        {path && (
          <li>
            <a href={href(name, path.split("/").slice(0, -1).join("/"))} aria-label="parent folder">
              <Icon dir />
              ..
            </a>
          </li>
        )}
        {[...dirs].map((d) => (
          <li key={d}>
            <a href={href(name, prefix + d)}>
              <Icon dir />
              {d}
            </a>
          </li>
        ))}
        {files.map((f) => (
          <li key={f}>
            <a href={href(name, prefix + f)}>
              <Icon />
              {f}
            </a>
          </li>
        ))}
      </ul>
      {/* The README is Henry's own file, rendered server side. */}
      {!path && data.readme_html && (
        <section className="box readme">
          <div className="box-head">README.md</div>
          <article className="readme-body" dangerouslySetInnerHTML={{ __html: data.readme_html }} />
        </section>
      )}
    </>
  );
}

function Icon({ dir }: { dir?: boolean }) {
  return (
    <svg className={dir ? "icon dir" : "icon"} viewBox="0 0 16 16" aria-hidden="true">
      {dir ? (
        <path d="M1.5 3.5a1 1 0 0 1 1-1h3.6l1.5 1.5h5.9a1 1 0 0 1 1 1v7.5a1 1 0 0 1-1 1h-11a1 1 0 0 1-1-1z" />
      ) : (
        <path d="M3.5 1.5h6l3 3v10h-9zM9.5 1.5v3h3" />
      )}
    </svg>
  );
}

// "#L12" selects line 12, like a repo browser.
function useHash() {
  const [hash, setHash] = useState(location.hash);
  useEffect(() => {
    const update = () => setHash(location.hash);
    addEventListener("hashchange", update);
    return () => removeEventListener("hashchange", update);
  }, []);
  return hash;
}

function FileView({ project, path, crumbs }: { project: string; path: string; crumbs: ReactNode }) {
  const encoded = path.split("/").map(encodeURIComponent).join("/");
  const { data, error } = useApi<CodeFile>(`/api/code/${project}/file/${encoded}`);
  const hash = useHash();
  const [copied, setCopied] = useState(false);
  const table = useRef<HTMLTableElement>(null);
  // Pygments wraps each line in a span, one per text line.
  const lines = data?.html?.split("\n").slice(0, -1);
  useEffect(() => {
    // The rows render after the page loads, so the browser cannot jump to the anchor itself.
    if (lines) document.getElementById(location.hash.slice(1))?.scrollIntoView();
  }, [data]);
  if (error)
    return (
      <>
        {crumbs}
        <p className="muted">file not found</p>
      </>
    );
  if (!data) return crumbs;
  const raw = `/code-raw/${project}/${encoded}`;
  const copy = () => {
    const text = [...(table.current?.querySelectorAll(".line") ?? [])].map((l) => l.textContent).join("\n");
    navigator.clipboard.writeText(text + "\n").then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  };
  return (
    <>
      {crumbs}
      <section className="box file">
        <div className="box-head">
          <span className="muted">{lines ? `${lines.length} lines` : path.split("/").pop()}</span>
          <span className="actions">
            {lines && (
              <button type="button" onClick={copy}>
                {copied ? "copied" : "copy"}
              </button>
            )}
            <a href={raw}>raw</a>
          </span>
        </div>
        {IMAGE.test(path) ? (
          <img className="code-image" src={raw} alt={path} />
        ) : !lines ? (
          <p className="muted binary">binary file</p>
        ) : (
          <div className="scroll">
            <table className="code" ref={table}>
              <tbody>
                {lines.map((line, i) => (
                  <tr key={i} id={`L${i + 1}`} className={hash === `#L${i + 1}` ? "hl" : undefined}>
                    <td>
                      <a href={`#L${i + 1}`}>{i + 1}</a>
                    </td>
                    {/* Highlighted server side by Pygments, which escapes the source. */}
                    <td dangerouslySetInnerHTML={{ __html: line }} />
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </>
  );
}
