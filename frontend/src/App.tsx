import { type ReactNode, useEffect, useRef, useState } from "react";

type Summary = { slug: string; title: string; excerpt: string | null; tags: string[]; draft: boolean };
type Post = Summary & { html: string };
type Now = { html: string };
type Project = { name: string; files: string[]; readme_html: string | null };
type CodeFile = { path: string; html?: string; binary?: true };

// shortcut: plain links and pathname matching, no router. Add react-router when there are more pages.
const slug = location.pathname.match(/^\/posts\/([a-z0-9-]+)\/?$/)?.[1];
// /code/<project>, /code/<project>/<path>, where the path is a folder or a file.
// There is no project index: Henry links to code from his posts.
const code = location.pathname.match(/^\/code\/([a-z0-9-]+)(?:\/(.+?))?\/?$/);
const now = /^\/now\/?$/.test(location.pathname);
// /?tag=<tag> filters the post list.
const tag = new URLSearchParams(location.search).get("tag");
const IMAGE = /\.(png|jpe?g|webp|gif|svg)$/i;

export default function App() {
  // Project pages are wider, like a repo browser, so long code lines fit.
  return <main className={code ? "wide" : undefined}>{page()}</main>;
}

function page() {
  if (slug) return <PostPage slug={slug} />;
  if (code) return <ProjectPage name={code[1]} path={code[2] ? decodeURIComponent(code[2]) : ""} />;
  return (
    <>
      <Header />
      {now ? <NowPage /> : <PostList />}
    </>
  );
}

// The "now" link shows only once Henry has written pages/now.md.
function Header() {
  const { data } = useApi<Now>("/api/now");
  return (
    <header>
      <a href="/">neenerweener</a>
      {(data || now) && (
        <a className="nav" href="/now" aria-current={now ? "page" : undefined}>
          now
        </a>
      )}
    </header>
  );
}

function NowPage() {
  const { data, error } = useApi<Now>("/api/now");
  useEffect(() => {
    document.title = "now";
  }, []);
  if (error) return <p className="muted">nothing here yet</p>;
  if (!data) return null;
  return (
    <article>
      {/* Henry's own file, rendered server side. */}
      <div dangerouslySetInnerHTML={{ __html: data.html }} />
    </article>
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

function Tags({ tags }: { tags: string[] }) {
  if (!tags.length) return null;
  return (
    <span className="tags">
      {tags.map((t) => (
        <a key={t} href={`/?tag=${encodeURIComponent(t)}`}>
          #{t}
        </a>
      ))}
    </span>
  );
}

function PostList() {
  const { data, error } = useApi<Summary[]>("/api/posts");
  if (error) return <p className="muted">could not load posts</p>;
  if (!data) return null;
  if (!data.length) return <p className="muted">no posts yet</p>;
  const counts = new Map<string, number>();
  for (const p of data) for (const t of p.tags) counts.set(t, (counts.get(t) ?? 0) + 1);
  const shown = tag ? data.filter((p) => p.tags.includes(tag)) : data;
  return (
    <>
      {counts.size > 0 && (
        <nav className="tag-filter" aria-label="tags">
          <a href="/" aria-current={tag ? undefined : "page"}>
            all
          </a>
          {[...counts].sort(([a], [b]) => a.localeCompare(b)).map(([t, n]) => (
            <a key={t} href={`/?tag=${encodeURIComponent(t)}`} aria-current={t === tag ? "page" : undefined}>
              #{t}
              <span className="muted"> {n}</span>
            </a>
          ))}
        </nav>
      )}
      {!shown.length && <p className="muted">no posts with this tag</p>}
      <ul className="posts">
        {shown.map((p) => (
          <li key={p.slug}>
            <div className="post-info">
              <a href={`/posts/${p.slug}`}>{p.title}</a>
              {p.excerpt && <div className="excerpt muted">{p.excerpt}</div>}
              <Tags tags={p.tags} />
            </div>
            {p.draft && <span className="draft muted">draft</span>}
          </li>
        ))}
      </ul>
    </>
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
          fontFamily: "Geist Mono Variable, monospace",
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
      <div className="meta">
        <Tags tags={data.tags} />
      </div>
      {/* Posts are Henry's own files, rendered server side. */}
      <div ref={body} dangerouslySetInnerHTML={{ __html: data.html }} />
    </article>
  );
}

const href = (project: string, path: string) =>
  `/code/${project}${path && "/" + path.split("/").map(encodeURIComponent).join("/")}`;

// project / dir / file, every segment but the last a link.
function Crumbs({ project, path }: { project: string; path: string }) {
  const parts = path ? path.split("/") : [];
  return (
    <h1 className="crumbs">
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

// Folders and files directly inside `dir`, folders first, both sorted. Folders are derived from file paths.
function children(files: string[], dir: string) {
  const prefix = dir && dir + "/";
  const dirs = new Set<string>();
  const leaves: string[] = [];
  for (const file of files) {
    if (!file.startsWith(prefix)) continue;
    const [first, ...rest] = file.slice(prefix.length).split("/");
    if (rest.length) dirs.add(first);
    else leaves.push(first);
  }
  return { dirs: [...dirs].sort(), files: leaves.sort() };
}

// The left file tree, like a repo browser. Folders on the way to the open path start expanded.
function Tree({ project, files, path }: { project: string; files: string[]; path: string }) {
  const [open, setOpen] = useState(() => {
    const parts = path.split("/");
    return new Set(parts.map((_, i) => parts.slice(0, i + 1).join("/")));
  });
  const toggle = (dir: string) =>
    setOpen((prev) => {
      const next = new Set(prev);
      if (!next.delete(dir)) next.add(dir);
      return next;
    });
  const level = (dir: string, depth: number): ReactNode => {
    const { dirs, files: leaves } = children(files, dir);
    const prefix = dir && dir + "/";
    const indent = { paddingLeft: `${0.75 + depth * 1}rem` };
    return (
      <ul>
        {dirs.map((d) => {
          const full = prefix + d;
          return (
            <li key={d}>
              <button
                type="button"
                style={indent}
                aria-expanded={open.has(full)}
                aria-current={full === path ? "page" : undefined}
                onClick={() => toggle(full)}
              >
                {d}/
              </button>
              {open.has(full) && level(full, depth + 1)}
            </li>
          );
        })}
        {leaves.map((f) => (
          <li key={f}>
            <a
              href={href(project, prefix + f)}
              style={indent}
              aria-current={prefix + f === path ? "page" : undefined}
            >
              {f}
            </a>
          </li>
        ))}
      </ul>
    );
  };
  return (
    <nav className="tree" aria-label="files">
      <a className="tree-root" href={href(project, "")} aria-current={path ? undefined : "page"}>
        {project}
      </a>
      {level("", 0)}
    </nav>
  );
}

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
  return (
    <div className="repo">
      <Tree project={name} files={data.files} path={path} />
      <div className="repo-main">
        {crumbs}
        {data.files.includes(path) ? (
          <FileView project={name} path={path} />
        ) : (
          <Folder project={data} path={path} />
        )}
      </div>
    </div>
  );
}

function Folder({ project, path }: { project: Project; path: string }) {
  const { dirs, files } = children(project.files, path);
  if (!dirs.length && !files.length) return <p className="muted">file not found</p>;
  const prefix = path && path + "/";
  return (
    <>
      <ul className="box listing">
        {path && (
          <li>
            <a href={href(project.name, path.split("/").slice(0, -1).join("/"))} aria-label="parent folder">
              ..
            </a>
          </li>
        )}
        {dirs.map((d) => (
          <li key={d}>
            <a href={href(project.name, prefix + d)}>{d}/</a>
          </li>
        ))}
        {files.map((f) => (
          <li key={f}>
            <a href={href(project.name, prefix + f)}>{f}</a>
          </li>
        ))}
      </ul>
      {/* The README is Henry's own file, rendered server side. */}
      {!path && project.readme_html && (
        <section className="box readme">
          <div className="box-head">README.md</div>
          <article className="readme-body" dangerouslySetInnerHTML={{ __html: project.readme_html }} />
        </section>
      )}
    </>
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

function FileView({ project, path }: { project: string; path: string }) {
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
    return <p className="muted">file not found</p>;
  if (!data) return null;
  const raw = `/code-raw/${project}/${encoded}`;
  const copy = () => {
    const text = [...(table.current?.querySelectorAll(".line") ?? [])].map((l) => l.textContent).join("\n");
    navigator.clipboard.writeText(text + "\n").then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  };
  return (
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
  );
}
