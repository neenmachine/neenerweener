import { type ComponentType, type MouseEvent, type ReactNode, lazy, Suspense, useEffect, useRef, useState } from "react";
// Types only: the admin code itself stays out of the public chunk.
import type { EntryRow, Send } from "./EntryEditor";
import { fetchWithSignal } from "./request";
import type { Entry, Media, Profile, Timeline, Zone } from "./timelineTypes";
import CanvasPage, { type CanvasSnapshot } from "./CanvasPage";
import ForumPage from "./ForumPage";
import ForumThreadPage from "./ForumThreadPage";

type Summary = { slug: string; title: string; excerpt: string | null; tags: string[]; draft: boolean };
type Post = Summary & { html: string };
type Project = { name: string; files: string[]; readme_html: string | null };
type CodeFile = { path: string; html?: string; binary?: true };

// The server matches the URL to a page (backend/routes.py) and sends it in the preload; the client has no route list.
type Page =
  | { kind: "home" }
  | { kind: "timeline" }
  | { kind: "forum" }
  | { kind: "forum-thread"; thread_id: string }
  | { kind: "canvas"; chrome?: false }
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
          <Footer />
        </main>
      );
    case "admin":
      // Admin draws its own main: signed in, the footer sits in the shell's content column.
      return (
        // The signed-out look Admin renders first, so the page never shows the footer alone while the chunk loads.
        <Suspense
          fallback={
            <main>
              <article className="admin">
                <AdminHead />
              </article>
              <Footer />
            </main>
          }
        >
          <Admin />
        </Suspense>
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
    case "canvas": {
      const canvas = <CanvasPage snapshot={preloaded["/api/canvas"] as CanvasSnapshot} initialEvents={(preloaded["/api/canvas/events"] as { events: never[] })?.events} initialNow={preloaded.canvas_now as string | undefined} />;
      return page.chrome === false ? canvas : <Shell>{canvas}</Shell>;
    }
    case "forum":
      return <Shell><ForumPage initial={preloaded["/api/forum"] as never} /></Shell>;
    case "forum-thread":
      return <Shell><ForumThreadPage id={page.thread_id} after={new URLSearchParams(location.search).get("after") ?? undefined} initial={preloaded[`/api/forum/threads/${page.thread_id}`] as never} status={preloaded.forum_status as "ready" | "not-found" | "unavailable" | undefined} /></Shell>;
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

// The signed-out admin heading, here in the main chunk so the Suspense fallback does not need the Admin chunk.
// Mirrors ADMIN_SHELL in backend/app.py.
export function AdminHead() {
  return (
    <div className="admin-head">
      <h1>admin</h1>
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
export function Footer() {
  const current = page.kind === "home" && !tag ? "home" : page.kind === "timeline" ? "timeline" : page.kind === "canvas" ? "canvas" : page.kind === "forum" || page.kind === "forum-thread" ? "forum" : null;
  return (
    <footer>
      <a href="/" aria-current={current === "home" ? "page" : undefined}>
        neenerweener
      </a>
      <a href="/timeline" aria-current={current === "timeline" ? "page" : undefined}>
        timeline
      </a>
      <a href="/canvas" aria-current={current === "canvas" ? "page" : undefined}>canvas</a>
      <a href="/forum" aria-current={current === "forum" ? "page" : undefined}>agent forum</a>
      <a href="/feed.xml">rss</a>
    </footer>
  );
}

// Newest first. The first page comes from the preload; "load more" fetches older pages by cursor.
function TimelinePage() {
  const { data, error } = useApi<Timeline>("/api/timeline");
  // The server always preloads it, with its defaults if the profile could not be read.
  const profile = useApi<Profile>("/api/profile").data;
  const [older, setOlder] = useState<Timeline[]>([]);
  // Entries the admin posted from this page; the preload and the cursor stay as the server sent them.
  const [created, setCreated] = useState<Entry[]>([]);
  // The admin's edits and deletes, applied over every source, so load more never brings back an old version.
  const [updated, setUpdated] = useState<Record<number, Entry>>({});
  const [deleted, setDeleted] = useState<Set<number>>(new Set());
  const [admin, expire] = useAdmin();
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  // What the always-mounted status region says, so screen readers hear loading, failure and progress.
  const [status, setStatus] = useState("");
  // createRoot replaces the server markup, so the new entry is never the browser's :target; track the hash.
  const [hash, setHash] = useState(location.hash);
  // A ref, not state: two activations in the same task both see busy=false.
  const inFlight = useRef(false);
  // Load more's fetch ends with the page; made in the effect, not in useRef: strict mode replays it.
  const life = useRef<AbortController>(null!);
  useEffect(() => {
    const c = (life.current = new AbortController());
    return () => c.abort();
  }, []);
  const button = useRef<HTMLAnchorElement>(null);
  const list = useRef<HTMLDivElement>(null);
  const empty = useRef<HTMLParagraphElement>(null);
  // Index of the entry to focus after the last page removes the focused button.
  const focusAt = useRef<number | null>(null);
  // Index of the entry whose edit button gets focus after a delete removes the focused one.
  const editAt = useRef<number | null>(null);
  useEffect(() => {
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
    const links = list.current?.querySelectorAll<HTMLElement>(".timeline-entry header a");
    const link = links?.[Math.min(focusAt.current, links.length - 1)];
    (link ?? empty.current)?.focus();
    focusAt.current = null;
  }, [older]);
  useEffect(() => {
    if (editAt.current === null) return;
    const buttons = list.current?.querySelectorAll<HTMLElement>("[data-action=edit]");
    (buttons?.[Math.min(editAt.current, buttons.length - 1)] ?? document.querySelector<HTMLElement>("form.admin textarea"))?.focus();
    editAt.current = null;
  }, [deleted]);
  const heading = <h1>timeline</h1>;
  if (error)
    return (
      <article>
        {heading}
        <p className="muted">could not load timeline</p>
      </article>
    );
  if (!data || !profile) return null;
  // The first page's clock for every entry, so one screen has one now and a new entry says "now".
  const now = data.now;
  const pages = [data, ...older];
  const cursor = pages[pages.length - 1].next_cursor;
  const seen = new Set<number>();
  const entries = [...created, ...pages.flatMap((p) => p.entries)]
    .filter((e) => !seen.has(e.id) && !!seen.add(e.id) && !deleted.has(e.id))
    .map((e) => updated[e.id] ?? e);
  const loadMore = (event: MouseEvent<HTMLAnchorElement>) => {
    event.preventDefault();
    if (inFlight.current || cursor === null) return;
    inFlight.current = true;
    setBusy(true);
    setFailed(false);
    setStatus("loading");
    fetchWithSignal(`/api/timeline?before=${cursor}`, life.current.signal)
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
      {admin && <admin.Composer profile={profile} onCreated={(e) => setCreated((prev) => [e, ...prev])} onExpired={expire} />}
      {/* A page can be empty and still have older entries (the server skips bad rows); only the end is empty. */}
      {!entries.length && cursor === null && (
        <p className="muted" tabIndex={-1} ref={empty}>
          nothing here yet
        </p>
      )}
      {/* Mirrors timeline_markup in backend/app.py, so the swap from the server markup is not visible. */}
      {/* "admin" gives the edit forms and controls the /admin form styles. */}
      <div className={admin ? "timeline admin" : "timeline"} ref={list}>
        {entries.map((e) => {
          const className = hash === `#entry-${e.id}` ? "timeline-entry target" : "timeline-entry";
          return admin ? (
            <admin.EntryRow
              key={e.id}
              entry={e}
              profile={profile}
              now={now}
              className={className}
              href={`#entry-${e.id}`}
              send={admin.send}
              onSaved={(saved, message) => {
                setUpdated((prev) => ({ ...prev, [saved.id]: saved }));
                setStatus(message);
              }}
              onGone={(id, message) => {
                if (list.current?.contains(document.activeElement)) editAt.current = entries.findIndex((x) => x.id === id);
                setDeleted((prev) => new Set(prev).add(id));
                setStatus(message);
              }}
              expire={expire}
            />
          ) : (
            <TimelineEntry key={e.id} entry={e} profile={profile} now={now} className={className} />
          );
        })}
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
// Mirror of PAGE_MAX_WIDTH, SINGLE_GUTTER and GRID_SIZES in the backend, which derives them from index.css.
const PAGE_MAX_WIDTH = 780;
const SINGLE_GUTTER = 95.5;
const GRID_SIZES = "(max-width: 780px) calc(50vw - 48.75px), 341.25px";
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
// One entry as timeline_markup in backend/app.py renders it: the avatar, then name, time, body and images in the
// column beside it. now is the page's clock. The admin editor (lazy) passes body to swap in its edit form, actions
// for its footer buttons, and children after the footer.
export function TimelineEntry({
  entry,
  profile,
  now,
  className = "timeline-entry",
  href = `#entry-${entry.id}`,
  body,
  actions,
  children,
}: {
  entry: Entry;
  profile: Profile;
  now: string;
  className?: string;
  href?: string;
  body?: ReactNode;
  actions?: ReactNode;
  children?: ReactNode;
}) {
  const created = full(entry.created_at, entry.created_zone);
  return (
    <article className={className} id={`entry-${entry.id}`}>
      <Avatar profile={profile} />
      <div className="entry-main">
        <header>
          <span className="name">{profile.display_name}</span>
          {profile.handle && <span className="handle">@{profile.handle}</span>}
          {/* One unit, so the time never wraps away from its separator. */}
          <span className="when">
            <span aria-hidden="true">·</span>
            <a href={href} aria-label={created}>
              <time dateTime={new Date(entry.created_at).toISOString()} title={created}>
                {compact(entry.created_at, now)}
              </time>
            </a>
            {entry.edited_at && entry.edited_zone && (
              <>
                <span aria-hidden="true">·</span>
                <span className="edited" title={full(entry.edited_at, entry.edited_zone)}>
                  edited<span className="sr-only"> {full(entry.edited_at, entry.edited_zone)}</span>
                </span>
              </>
            )}
          </span>
        </header>
        {body ?? (
          <>
            {/* Rendered server side from Henry's entry, with raw HTML disabled. */}
            <div className="entry-body" dangerouslySetInnerHTML={{ __html: entry.html }} />
            <EntryMedia media={entry.media} />
          </>
        )}
        {actions && <footer>{actions}</footer>}
        {children}
      </div>
    </article>
  );
}

// The poster's picture, or a neutral circle with the server's initial. Hidden from screen readers: the name beside
// it says who posted. avatar_markup in backend/app.py renders the same markup.
// The admin shell shows a 24px one beside the name; the profile page an 88px preview (72px on phones, in CSS).
export function Avatar({ profile, size = 44 }: { profile: Profile; size?: 24 | 44 | 88 }) {
  const className = { 24: "avatar small", 44: "avatar", 88: "avatar preview" }[size];
  if (!profile.avatar_id)
    return (
      <div className={className} aria-hidden="true">
        {profile.avatar_initial}
      </div>
    );
  const base = `/media/${profile.avatar_id}`;
  return (
    <img
      className={className}
      sizes={`${size}px`}
      width={size}
      height={size}
      alt=""
      aria-hidden="true"
      srcSet={`${base}/sm.webp 96w, ${base}/lg.webp 192w`}
      src={`${base}/sm.webp`}
    />
  );
}

export function EntryMedia({ media }: { media: Media[] }) {
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
              sizes={
                media.length === 1
                  ? `(max-width: ${Math.min(width + SINGLE_GUTTER, PAGE_MAX_WIDTH)}px) calc(100vw - ${SINGLE_GUTTER}px), ` +
                    `${Math.min(width, PAGE_MAX_WIDTH - SINGLE_GUTTER)}px`
                  : GRID_SIZES
              }
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

type ComposerProps = { profile: Profile; onCreated: (entry: Entry) => void; onExpired: () => void };
// The admin parts of /timeline, all from lazy chunks: the composer, the per-entry controls, and their request.
type AdminKit = { Composer: ComponentType<ComposerProps>; EntryRow: typeof EntryRow; send: Send };

// The admin's composer and entry controls, or null. The server sends the same page to everyone; a non-secret
// hint cookie (set at login) says whether to check the session, so visitors pay no request. Only a signed-in
// session loads the admin chunks; the real auth is the session cookie and CSRF token on each request.
// expire (any 401) drops them all; adminApi has cleared the hint by then.
function useAdmin(): [AdminKit | null, () => void] {
  const [kit, setKit] = useState<AdminKit | null>(null);
  const expire = () => setKit(null);
  useEffect(() => {
    if (!/(^|;\s*)admin_hint=1(;|$)/.test(document.cookie)) return;
    // Aborted on unmount and on the strict mode replay: an old check never writes the shared csrf token or hint.
    const c = new AbortController();
    import("./adminApi")
      .then((api) => api.checkSession(c.signal))
      .then((r) => (r.ok && r.data.authenticated ? Promise.all([import("./TimelineComposer"), import("./EntryEditor")]) : undefined))
      .then((m) => !c.signal.aborted && m && setKit({ Composer: m[0].default, EntryRow: m[1].EntryRow, send: m[1].sendOrExpire(expire) }))
      .catch(() => {});
    return () => c.abort();
  }, []);
  return [kit, expire];
}

// The site's one time zone; compact_timestamp and full_timestamp in backend/app.py format the same fields.
const toronto = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/Toronto",
  year: "numeric",
  month: "long",
  day: "numeric",
  hour: "numeric",
  minute: "2-digit",
  hour12: true,
});
const fields = (iso: string) =>
  Object.fromEntries(toronto.formatToParts(new Date(iso)).map((p) => [p.type, p.value])) as Record<Intl.DateTimeFormatPartTypes, string>;

// Age against the page's now (never the wall clock): "now", "12m", "3h", then the Toronto date, "nov 7" in now's
// Toronto year and "nov 7, 2025" before it. The full date and time are in the tooltip.
export function compact(iso: string, now: string) {
  const age = (Date.parse(now) - Date.parse(iso)) / 1000;
  if (age < 60) return "now";
  if (age < 3600) return `${Math.floor(age / 60)}m`;
  if (age < 86400) return `${Math.floor(age / 3600)}h`;
  const d = fields(iso);
  return `${d.month.slice(0, 3).toLowerCase()} ${d.day}${d.year === fields(now).year ? "" : `, ${d.year}`}`;
}

// "November 7, 2025 at 10:04 AM EST", with the zone the server named for this time.
export function full(iso: string, zone: Zone) {
  const d = fields(iso);
  return `${d.month} ${d.day}, ${d.year} at ${d.hour}:${d.minute} ${d.dayPeriod} ${zone}`;
}

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
    const c = new AbortController();
    fetchWithSignal(url, c.signal)
      .then((r) => (r.ok ? r.json() : Promise.reject()))
      .then(
        (d) => !c.signal.aborted && setData(d),
        () => !c.signal.aborted && setError(true),
      );
    return () => c.abort();
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
