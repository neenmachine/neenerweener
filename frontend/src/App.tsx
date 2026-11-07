import { type ComponentType, type MouseEvent, type ReactNode, lazy, Suspense, useEffect, useRef, useState } from "react";
// Type only: the adminApi code itself stays out of the public chunk.
import type { Entry, Media } from "./adminApi";

type Summary = { slug: string; title: string; excerpt: string | null; tags: string[]; draft: boolean };
type Post = Summary & { html: string };
type Timeline = { entries: Entry[]; next_cursor: number | null };
type Project = { name: string; files: string[]; readme_html: string | null };
type CodeFile = { path: string; html?: string; binary?: true };

// The server matches the URL to a page (backend/routes.py) and sends it in the preload; the client has no route list.
type Page =
  | { kind: "home" }
  | { kind: "timeline" }
  | { kind: "admin" }
  | { kind: "post"; slug: string }
  | { kind: "code"; project: string; path?: string }
  | { kind: "not-found"; label: string; back: boolean };
// Private and unlinked; its code is a separate chunk that public pages never load.
const Admin = lazy(() => import("./Admin"));
// /?tag=<tag> filters the post list.
const tag = new URLSearchParams(location.search).get("tag");
const IMAGE = /\.(png|jpe?g|webp|gif|svg)$/i;

// API responses the server put into the page (backend `preload`), so the first render needs no fetch.
// null means the server could not load it.
const preloaded: Record<string, unknown> = JSON.parse(document.getElementById("preload")?.textContent || "{}");
const page = (preloaded.page as Page | undefined) ?? { kind: "not-found", label: "not found", back: false };

export default function App() {
  switch (page.kind) {
    case "not-found":
      return <NotFound label={page.label} back={page.back} />;
    case "code":
      // Project pages are wider, like a repo browser, so long code lines fit.
      return (
        <main className="wide">
          <ProjectPage name={page.project} path={page.path ?? ""} />
        </main>
      );
    case "admin":
      return (
        <Shell>
          {/* The shell Admin renders first, so the page never shows the footer alone while the chunk loads. */}
          <Suspense
            fallback={
              <article className="admin">
                <AdminHead />
              </article>
            }
          >
            <Admin />
          </Suspense>
        </Shell>
      );
    case "post":
      return (
        <Shell>
          <PostPage slug={page.slug} />
        </Shell>
      );
    case "timeline":
      return (
        <Shell>
          <TimelinePage />
        </Shell>
      );
    case "home":
      return (
        <Shell>
          <h1 className="sr-only">neenerweener</h1>
          <PostList />
        </Shell>
      );
    default:
      // A kind this bundle does not know (a newer server) shows not found; `satisfies never` keeps the switch exhaustive.
      page satisfies never;
      return <NotFound label="not found" back={false} />;
  }
}

// The admin heading, here in the main chunk so the Suspense fallback does not need the Admin chunk.
// Mirrors ADMIN_SHELL in backend/app.py.
export function AdminHead({ children }: { children?: ReactNode }) {
  return (
    <div className="admin-head">
      <h1>admin</h1>
      {children}
    </div>
  );
}

function Shell({ children }: { children: ReactNode }) {
  return (
    <main>
      {children}
      <Footer />
    </main>
  );
}

// Mirrors not_found_page in backend/app.py, so the swap from the server markup is not visible.
function NotFound({ label, back }: { label: string; back: boolean }) {
  return (
    <main>
      {back ? (
        <div className="has-back">
          <Back href="/" />
          <h1 className="muted">{label}</h1>
        </div>
      ) : (
        <>
          <h1 className="muted">{label}</h1>
          <a href="/">neenerweener</a>
        </>
      )}
      <Footer />
    </main>
  );
}

// Mirrors site_footer in backend/page_markup.py.
function Footer() {
  const current = page.kind === "home" && !tag ? "home" : page.kind === "timeline" ? "timeline" : null;
  return (
    <footer>
      <a href="/" aria-current={current === "home" ? "page" : undefined}>
        neenerweener
      </a>
      <a href="/timeline" aria-current={current === "timeline" ? "page" : undefined}>
        timeline
      </a>
      <a href="/feed.xml">feed</a>
    </footer>
  );
}

// Newest first. The first page comes from the preload; "load more" fetches older pages by cursor.
function TimelinePage() {
  const { data, error } = useApi<Timeline>("/api/timeline");
  const [older, setOlder] = useState<Timeline[]>([]);
  // Entries the admin posted from this page; the preload and the cursor stay as the server sent them.
  const [created, setCreated] = useState<Entry[]>([]);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  // What the always-mounted status region says, so screen readers hear loading, failure and progress.
  const [status, setStatus] = useState("");
  // createRoot replaces the server markup, so the new entry is never the browser's :target; track the hash.
  const [hash, setHash] = useState(location.hash);
  // A ref, not state: two activations in the same task both see busy=false.
  const inFlight = useRef(false);
  const button = useRef<HTMLAnchorElement>(null);
  const list = useRef<HTMLDivElement>(null);
  const empty = useRef<HTMLParagraphElement>(null);
  // Index of the entry to focus after the last page removes the focused button.
  const focusAt = useRef<number | null>(null);
  useEffect(() => {
    document.title = "timeline";
    const onHash = () => setHash(location.hash);
    addEventListener("hashchange", onHash);
    return () => removeEventListener("hashchange", onHash);
  }, []);
  useEffect(() => {
    // Entries render after the page loads, so the browser cannot jump to /timeline#entry-<id> itself.
    if (data && location.hash) document.getElementById(location.hash.slice(1))?.scrollIntoView();
  }, [data]);
  useEffect(() => {
    if (focusAt.current === null) return;
    const links = list.current?.querySelectorAll<HTMLElement>(".timeline-entry > footer > a");
    const link = links?.[Math.min(focusAt.current, links.length - 1)];
    (link ?? empty.current)?.focus();
    focusAt.current = null;
  }, [older]);
  const heading = <h1>timeline</h1>;
  if (error)
    return (
      <article>
        {heading}
        <p className="muted">could not load timeline</p>
      </article>
    );
  if (!data) return null;
  const pages = [data, ...older];
  const cursor = pages[pages.length - 1].next_cursor;
  const seen = new Set<number>();
  const entries = [...created, ...pages.flatMap((p) => p.entries)].filter((e) => !seen.has(e.id) && !!seen.add(e.id));
  const loadMore = (event: MouseEvent<HTMLAnchorElement>) => {
    event.preventDefault();
    if (inFlight.current || cursor === null) return;
    inFlight.current = true;
    setBusy(true);
    setFailed(false);
    setStatus("loading");
    fetch(`/api/timeline?before=${cursor}`)
      .then((r) => (r.ok ? r.json() : Promise.reject()))
      .then(
        (page: Timeline) => {
          const added = page.entries.filter((e) => !seen.has(e.id)).length;
          // The button goes away with the last page; keep keyboard focus on the new entries, not <body>.
          if (page.next_cursor === null && document.activeElement === button.current) focusAt.current = entries.length;
          setOlder((prev) => [...prev, page]);
          setStatus(page.next_cursor === null ? `${added} more loaded, end of timeline` : `${added} more loaded`);
        },
        () => {
          setFailed(true);
          setStatus("could not load more");
        },
      )
      .finally(() => {
        inFlight.current = false;
        setBusy(false);
      });
  };
  return (
    <article>
      {heading}
      <Compose onCreated={(e) => setCreated((prev) => [e, ...prev])} />
      {/* A page can be empty and still have older entries (the server skips bad rows); only the end is empty. */}
      {!entries.length && cursor === null && (
        <p className="muted" tabIndex={-1} ref={empty}>
          nothing here yet
        </p>
      )}
      {/* Mirrors timeline_markup in backend/app.py, so the swap from the server markup is not visible. */}
      <div className="timeline" ref={list}>
        {entries.map((e) => (
          <article
            key={e.id}
            className={hash === `#entry-${e.id}` ? "timeline-entry target" : "timeline-entry"}
            id={`entry-${e.id}`}
          >
            {/* Rendered server side from Henry's entry, with raw HTML disabled. */}
            <div dangerouslySetInnerHTML={{ __html: e.html }} />
            <EntryMedia media={e.media} />
            <footer>
              <a href={`#entry-${e.id}`} aria-label={full(e.created_at)}>
                <time dateTime={new Date(e.created_at).toISOString()} title={full(e.created_at)}>
                  {compact(e.created_at)}
                </time>
              </a>
              {e.edited_at && (
                <span className="edited" title={full(e.edited_at)}>
                  edited<span className="sr-only"> {full(e.edited_at)}</span>
                </span>
              )}
            </footer>
          </article>
        ))}
      </div>
      {cursor !== null && (
        <div className="load-more">
          {/* aria-disabled, not disabled, so keyboard focus stays on the button while it loads. */}
          <a href={`/timeline?before=${cursor}`} ref={button} onClick={loadMore} aria-disabled={busy} aria-busy={busy}>
            {/* Both labels share one grid cell, so the button keeps the width of the longer one. */}
            <span hidden={busy}>load more</span>
            <span hidden={!busy}>loading</span>
          </a>
          {/* The status region below announces it. */}
          {failed && (
            <span className="muted" aria-hidden="true">
              could not load more
            </span>
          )}
        </div>
      )}
      <p className="sr-only" role="status">
        {status}
      </p>
    </article>
  );
}

// Mirror of media.fit and media_markup in the backend; see SINGLE_IMAGE_MAX_HEIGHT there.
const SM_EDGE = 800;
const SINGLE_IMAGE_MAX_HEIGHT = 512;
// Python's round(): a half goes to the even neighbour, so the numbers match the server markup.
const round = (x: number) => {
  const r = Math.round(x);
  return r - x === 0.5 && r % 2 ? r - 1 : r;
};
const smWidth = (width: number, height: number) => {
  const scale = SM_EDGE / Math.max(width, height);
  return scale >= 1 ? width : Math.max(1, round(width * scale));
};

// An entry's images like a tweet's: one keeps its shape, 2 to 4 fill a fixed grid, so nothing shifts on load.
function EntryMedia({ media }: { media: Media[] }) {
  if (!media.length) return null;
  return (
    <div className={`media media-${media.length}`}>
      {media.map((m) => {
        const base = `/media/${m.id}`;
        const sm = smWidth(m.width, m.height);
        let { width, height } = m;
        if (media.length === 1 && height > SINGLE_IMAGE_MAX_HEIGHT) {
          width = Math.max(1, round((width * SINGLE_IMAGE_MAX_HEIGHT) / height));
          height = SINGLE_IMAGE_MAX_HEIGHT;
        }
        return (
          <a key={m.id} href={`${base}/lg.webp`} aria-label={m.alt ? undefined : "open image"}>
            <img
              sizes={media.length === 1 ? `(max-width: 780px) 100vw, ${width}px` : "(max-width: 780px) 50vw, 371px"}
              width={width}
              height={height}
              alt={m.alt}
              loading="lazy"
              decoding="async"
              srcSet={`${base}/sm.webp ${sm}w` + (sm < m.width ? `, ${base}/lg.webp ${m.width}w` : "")}
              src={`${base}/sm.webp`}
            />
          </a>
        );
      })}
    </div>
  );
}

type ComposerProps = { onCreated: (entry: Entry) => void; onExpired: () => void };

// The admin's compose box, or nothing. The server sends the same page to everyone; a non-secret hint cookie
// (set at login) says whether to check the session, so visitors pay no request. Only a signed-in session
// loads the composer chunk; the real auth is the session cookie and CSRF token on the create request.
function Compose({ onCreated }: { onCreated: (entry: Entry) => void }) {
  const [Composer, setComposer] = useState<ComponentType<ComposerProps> | null>(null);
  useEffect(() => {
    if (!/(^|;\s*)admin_hint=1(;|$)/.test(document.cookie)) return;
    let live = true;
    import("./adminApi")
      .then((api) => api.checkSession())
      .then((r) => (r.ok && r.data.authenticated ? import("./TimelineComposer") : undefined))
      .then((m) => live && m && setComposer(() => m.default))
      .catch(() => {});
    return () => {
      live = false;
    };
  }, []);
  return Composer && <Composer onCreated={onCreated} onExpired={() => setComposer(null)} />;
}

// "nov 7" this year, "nov 7, 2025" before; the full date and time are in the tooltip.
export function compact(iso: string) {
  const d = new Date(iso);
  const year = d.getUTCFullYear() === new Date().getUTCFullYear() ? undefined : "numeric";
  return d.toLocaleDateString("en", { month: "short", day: "numeric", year, timeZone: "UTC" }).toLowerCase();
}

export const full = (iso: string) => new Date(iso).toLocaleString("en", { dateStyle: "long", timeStyle: "short", timeZone: "UTC" });

function Back({ href }: { href: string }) {
  return (
    <a className="back" href={href} aria-label="back">
      ←
    </a>
  );
}

function useApi<T>(url: string) {
  const known = url in preloaded;
  const [data, setData] = useState<T | undefined>(() => (preloaded[url] ?? undefined) as T | undefined);
  const [error, setError] = useState(() => known && preloaded[url] === null);
  useEffect(() => {
    if (known) return;
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
          {`#${t}`}
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
          {[...counts].sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0)).map(([t, n]) => (
            <a key={t} href={`/?tag=${encodeURIComponent(t)}`} aria-current={t === tag ? "page" : undefined}>
              {`#${t}`}<span className="muted"> {n}</span>
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
          theme: matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "neutral",
          fontFamily: "-apple-system, BlinkMacSystemFont, sans-serif",
        });
        mermaid.run({ nodes: diagrams });
      });
    }
    body.current?.querySelectorAll("pre:not(.mermaid)").forEach(addCopyButton);
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
      <div className="has-back">
        {back}
        <h1 className="muted">post not found</h1>
      </div>
    );
  if (!data) return null;
  return (
    <article>
      {/* The back link sits beside the h1, not in it, so the heading's name is the title alone. */}
      <div className="has-back post-head">
        {back}
        <h1>
          {data.title} {data.draft && <span className="draft muted">draft</span>}
        </h1>
      </div>
      <div className="meta">
        <Tags tags={data.tags} />
      </div>
      {/* Posts are Henry's own files, rendered server side. */}
      <div ref={body} dangerouslySetInnerHTML={{ __html: data.html }} />
    </article>
  );
}

// Wraps a post code block so a "copy" button can sit in its corner.
function addCopyButton(pre: Element) {
  if (pre.parentElement?.classList.contains("pre-wrap")) return;
  const wrap = document.createElement("div");
  wrap.className = "pre-wrap";
  pre.replaceWith(wrap);
  wrap.append(pre);
  const button = document.createElement("button");
  button.type = "button";
  button.className = "copy";
  button.textContent = "copy";
  button.onclick = () =>
    navigator.clipboard.writeText(pre.textContent ?? "").then(() => {
      button.textContent = "copied";
      setTimeout(() => (button.textContent = "copy"), 1500);
    });
  wrap.append(button);
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
        <Footer />
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
  if (!dirs.length && !files.length) return <><p className="muted">file not found</p><Footer /></>;
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
    return <><p className="muted">file not found</p><Footer /></>;
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
