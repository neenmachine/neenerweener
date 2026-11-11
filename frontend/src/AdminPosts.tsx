import { useEffect, useRef, useState } from "react";
import { compact } from "./App";
import type { Result } from "./adminApi";
import type { AdminPageProps } from "./adminPages";
import { Button, type Send } from "./EntryEditor";
import { previewDocument } from "./postPreview";
import { useRequest } from "./useRequest";

// Web posts (in the database, beside the read-only posts/ files): the list, then one post's editor with raw/preview,
// draft/live, delete and history. The server owns every rule; its errors show inline.

type Post = {
  id: number;
  title: string;
  slug: string;
  body_markdown: string;
  excerpt: string | null;
  status: "draft" | "live";
  published_on: string | null;
  version: number;
  deleted_at: string | null;
  tags: string[];
};
type Revision = { id: number; version: number; title: string; status: "draft" | "live"; recorded_at: string };

const DISCARD = "discard unsaved changes?";

export default function AdminPosts({ send, active }: AdminPageProps) {
  const [posts, setPosts] = useState<Post[]>([]);
  const [list, setList] = useState<"loading" | "ok" | "failed">("loading");
  const [error, setError] = useState("");
  const [open, setOpen] = useState<Post | "new" | null>(null);
  const { run } = useRequest("load");
  const heading = useRef<HTMLHeadingElement>(null);

  // quiet: a refresh keeps the shown list until the new one arrives.
  const load = async (quiet = false) => {
    if (!quiet) setList("loading");
    const r = await run((signal) => send<Post[]>("GET", "/api/admin/posts", undefined, signal));
    if (r.ok) setPosts(r.data);
    else if (r.status === 401) return;
    setError(r.ok ? "" : r.message);
    setList(r.ok ? "ok" : "failed");
  };
  // Each time the page is shown, since another tab may have changed the posts.
  useEffect(() => {
    if (active) load(list === "ok");
  }, [active]);

  const saved = (post: Post) => setPosts((prev) => [post, ...prev.filter((p) => p.id !== post.id)]);
  const close = () => {
    setOpen(null);
    heading.current?.focus();
  };

  return (
    <>
      <h1 tabIndex={-1} ref={heading}>
        posts
      </h1>
      {open !== null ? (
        <PostEditor key={open === "new" ? "new" : open.id} start={open === "new" ? null : open} send={send} onSaved={saved} onClose={close} />
      ) : (
        <section className="admin-section">
          <div className="admin-row">
            <Button onClick={() => setOpen("new")} label="new post" />
          </div>
          {list === "loading" && <p className="admin-state muted">loading</p>}
          {list === "failed" && (
            <div className="admin-state admin-row">
              <p className="error" role="alert">
                {error}
              </p>
              <Button onClick={() => load()} label="retry" />
            </div>
          )}
          {list === "ok" && posts.length === 0 && <p className="admin-state muted">nothing here yet</p>}
          {posts.length > 0 && (
            <ul className="admin-links">
              {posts.map((p) => (
                <li key={p.id}>
                  <button type="button" onClick={() => setOpen(p)}>
                    <span className="label">{p.title}</span>
                    <span className="muted">
                      {[stateOf(p), p.published_on, `/posts/${p.slug}`].filter(Boolean).join(" · ")}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </section>
      )}
    </>
  );
}

const stateOf = (p: Post) => (p.deleted_at ? "deleted" : p.status);

type Fields = { title: string; slug: string; published_on: string; excerpt: string; tags: string; body: string };
const fieldsOf = (p: Post | null): Fields => ({
  title: p?.title ?? "",
  slug: p?.slug ?? "",
  published_on: p?.published_on ?? "",
  excerpt: p?.excerpt ?? "",
  tags: p?.tags.join(", ") ?? "",
  body: p?.body_markdown ?? "",
});
const payload = (f: Fields) => ({
  title: f.title,
  slug: f.slug,
  body_markdown: f.body,
  excerpt: f.excerpt || null,
  tags: f.tags.split(","),
  published_on: f.published_on || null,
});

function PostEditor({ start, send, onSaved, onClose }: { start: Post | null; send: Send; onSaved: (p: Post) => void; onClose: () => void }) {
  const [post, setPost] = useState(start);
  const [fields, setFields] = useState(() => fieldsOf(start));
  const [mode, setMode] = useState<"raw" | "preview">("raw");
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState("");
  const [conflict, setConflict] = useState(false);
  const [status, setStatus] = useState("");
  // Save, publish, unpublish, delete, restore and reload: one request at a time for this post.
  const { run, busy } = useRequest("submit");
  const confirmButton = useRef<HTMLButtonElement>(null);
  const deleteButton = useRef<HTMLButtonElement>(null);
  const title = useRef<HTMLHeadingElement>(null);

  const dirty = JSON.stringify(fields) !== JSON.stringify(fieldsOf(post));
  const deleted = !!post?.deleted_at;
  // The browser asks before a reload or a closed tab drops unsaved text.
  useEffect(() => {
    if (!dirty) return;
    const ask = (e: BeforeUnloadEvent) => e.preventDefault();
    addEventListener("beforeunload", ask);
    return () => removeEventListener("beforeunload", ask);
  }, [dirty]);
  useEffect(() => {
    if (confirming) confirmButton.current?.focus();
  }, [confirming]);
  // A new or opened post: the list button that had focus is gone.
  useEffect(() => title.current?.focus(), []);
  const cancelDelete = () => {
    setConfirming(false);
    setTimeout(() => deleteButton.current?.focus()); // after the delete button is back
  };

  const set = (key: keyof Fields) => (e: { target: { value: string } }) => {
    setFields((f) => ({ ...f, [key]: e.target.value }));
    setStatus("");
    if (!conflict) setError(""); // a conflict's message stays beside its reload button
  };

  // The server owns every field rule ("invalid slug", "live post needs title and body", ...); its message shows inline.
  // Every request here answers with the post; a 409 (changed in another tab) keeps the fields as typed and offers reload.
  const call = async (request: (signal: AbortSignal) => Promise<Result<Post>>, message: string) => {
    const r = await run((signal) => {
      setError("");
      setConflict(false);
      setStatus("");
      return request(signal);
    });
    if (!r.ok) {
      if (r.status === 401) return; // the shell asks to log in again; the fields stay as they are
      setError(r.status === 404 ? "post not found" : r.message);
      setConflict(r.status === 409);
      return;
    }
    setPost(r.data);
    setFields(fieldsOf(r.data));
    onSaved(r.data);
    setStatus(message);
  };

  // A new post is created as a draft first; publishing it is then a second request.
  const save = (next: "draft" | "live", message: string) =>
    call(async (signal) => {
      let current = post;
      if (!current) {
        const created = await send<Post>("POST", "/api/admin/posts", payload(fields), signal);
        if (!created.ok || next === "draft") return created;
        current = created.data;
        if (!signal.aborted) {
          setPost(current);
          onSaved(current);
        }
      }
      return send<Post>("PATCH", `/api/admin/posts/${current.id}`, { ...payload(fields), status: next, version: current.version }, signal);
    }, message);

  const remove = async () => {
    await call((signal) => send<Post>("DELETE", `/api/admin/posts/${post!.id}`, { version: post!.version }, signal), "deleted");
    setConfirming(false);
    title.current?.focus(); // the delete buttons are gone
  };
  const restore = (revision: Revision) =>
    (!dirty || confirm(DISCARD)) &&
    call(
      (signal) => send<Post>("POST", `/api/admin/posts/${post!.id}/revisions/${revision.id}/restore`, { version: post!.version }, signal),
      "restored",
    );
  const reload = () =>
    (!dirty || confirm(DISCARD)) &&
    call((signal) => send<Post>("GET", `/api/admin/posts/${post!.id}`, undefined, signal), "");
  const back = () => (!dirty || confirm(DISCARD)) && onClose();

  const live = post?.status === "live";
  const field = (key: keyof Fields, label: string, type = "text") => (
    <div className={`field-${key}`}>
      <label htmlFor={`post-${key}`}>{label}</label>
      <input id={`post-${key}`} type={type} value={fields[key]} onChange={set(key)} readOnly={busy || deleted} />
    </div>
  );

  return (
    <>
      {/* Enter in a field saves (never publishes): the submit button is save or save draft. */}
      <form
        className="admin-section post-editor"
        onSubmit={(e) => {
          e.preventDefault();
          if (!deleted) save(live ? "live" : "draft", "saved");
        }}
      >
        <div className="admin-row">
          <Button onClick={back} label="back" />
          <h2 tabIndex={-1} ref={title}>
            {post ? post.title : "new post"}
          </h2>
          {post && <span className="muted">{stateOf(post)}</span>}
        </div>
        <div className="post-fields">
          {field("title", "title")}
          {field("slug", "slug")}
          {field("published_on", "date", "date")}
          {field("tags", "tags")}
          <div className="field-excerpt">
            <label htmlFor="post-excerpt">excerpt</label>
            <textarea id="post-excerpt" rows={2} value={fields.excerpt} onChange={set("excerpt")} readOnly={busy || deleted} />
          </div>
        </div>
        <div className="post-body">
          <div className="admin-row">
            <span className="label" id="post-markdown-label">
              markdown
            </span>
            <span className="mode-switch">
              <button type="button" className="button" aria-pressed={mode === "raw"} onClick={() => setMode("raw")}>
                raw
              </button>
              <button type="button" className="button" aria-pressed={mode === "preview"} onClick={() => setMode("preview")}>
                preview
              </button>
            </span>
          </div>
          {/* The textarea stays rendered under the preview, so its text, selection and undo history stay. */}
          <div className="markdown-stack">
            <textarea
              className={mode === "raw" ? "markdown" : "markdown covered"}
              aria-labelledby="post-markdown-label"
              inert={mode !== "raw"}
              value={fields.body}
              onChange={set("body")}
              readOnly={busy || deleted}
            />
            <Preview body={fields.body} shown={mode === "preview"} send={send} />
          </div>
        </div>
        <div className="save-row">
          <div className="admin-row">
            {!deleted && !confirming && (
              <span className="admin-actions">
                {live ? (
                  <>
                    <Button type="submit" busy={busy} label="save" busyLabel="saving" />
                    <Button onClick={() => save("draft", "unpublished")} busy={busy} label="unpublish" busyLabel="saving" />
                  </>
                ) : (
                  <>
                    <Button type="submit" busy={busy} label="save draft" busyLabel="saving" />
                    <Button onClick={() => save("live", "published")} busy={busy} label="publish" busyLabel="saving" />
                  </>
                )}
                {post && <Button ref={deleteButton} onClick={() => setConfirming(true)} off={busy} label="delete" />}
              </span>
            )}
            {confirming && (
              <span
                className="admin-actions"
                role="group"
                aria-label="delete post?"
                onKeyDown={(e) => e.key === "Escape" && !busy && cancelDelete()}
              >
                <Button ref={confirmButton} onClick={remove} busy={busy} label="confirm delete" busyLabel="deleting" danger />
                <Button onClick={cancelDelete} off={busy} label="cancel" />
              </span>
            )}
            {conflict && <Button onClick={reload} busy={busy} label="reload" busyLabel="loading" />}
            <span className="muted" role="status">
              {status}
            </span>
          </div>
          <p className="error" role="alert">
            {error}
          </p>
        </div>
      </form>
      {post && <History post={post} send={send} busy={busy} onRestore={restore} />}
    </>
  );
}

// The rendered post, from the server's renderer, only in the sandboxed frame (see postPreview.ts). Switching to
// preview renders at once; while it stays shown, a changed text renders after a pause, and a newer render cancels an
// older one.
function Preview({ body, shown, send }: { body: string; shown: boolean; send: Send }) {
  const { run, busy } = useRequest("load");
  const [doc, setDoc] = useState<string | null>(null);
  const [error, setError] = useState("");
  const rendered = useRef<string | null>(null);
  const wasShown = useRef(false);

  const render = async (text: string) => {
    const r = await run(async (signal) => {
      setError("");
      const p = await send<{ html: string }>("POST", "/api/admin/posts/preview", { body_markdown: text }, signal);
      return p.ok ? { ok: true as const, doc: await previewDocument(p.data.html) } : p;
    });
    if (!r.ok) return r.status !== 401 && setError(r.message);
    rendered.current = text;
    setDoc(r.doc);
  };
  useEffect(() => {
    const opening = !wasShown.current;
    wasShown.current = shown;
    if (!shown || rendered.current === body) return;
    const t = setTimeout(() => render(body), opening ? 0 : 400);
    return () => clearTimeout(t);
  }, [shown, body]);

  return (
    <div className="preview" hidden={!shown}>
      {busy && doc === null && <p className="admin-state muted">loading</p>}
      {error && (
        <div className="admin-state admin-row">
          <p className="error" role="alert">
            {error}
          </p>
          <Button onClick={() => render(body)} label="retry" />
        </div>
      )}
      {/* allow-scripts without allow-same-origin: an opaque origin; the document's own CSP blocks every script. */}
      {doc !== null && <iframe className="post-preview" sandbox="allow-scripts" srcDoc={doc} title="preview" />}
    </div>
  );
}

// Each recorded version of the post, newest first, reloaded after every change; restore makes it the current draft.
function History({ post, send, busy, onRestore }: { post: Post; send: Send; busy: boolean; onRestore: (r: Revision) => void }) {
  const [revisions, setRevisions] = useState<Revision[] | null>(null);
  const [now, setNow] = useState("");
  const [error, setError] = useState("");
  const { run } = useRequest("load");
  const load = async () => {
    const r = await run((signal) => send<Revision[]>("GET", `/api/admin/posts/${post.id}/revisions`, undefined, signal));
    if (!r.ok) return r.status !== 401 && setError(r.message);
    setError("");
    setNow(new Date().toISOString());
    setRevisions(r.data);
  };
  useEffect(() => {
    load();
  }, [post.id, post.version]);

  return (
    <section className="admin-section history">
      <h2>history</h2>
      {revisions === null && !error && <p className="admin-state muted">loading</p>}
      {error && (
        <div className="admin-state admin-row">
          <p className="error" role="alert">
            {error}
          </p>
          <Button onClick={load} label="retry" />
        </div>
      )}
      {revisions?.length === 0 && <p className="admin-state muted">nothing here yet</p>}
      {revisions && revisions.length > 0 && (
        <ul className="revisions">
          {revisions.map((r) => (
            <li key={r.id} className="admin-row">
              <span className="muted">
                {compact(r.recorded_at, now)} · v{r.version} · {r.status}
              </span>
              <span className="title">{r.title}</span>
              <Button onClick={() => onRestore(r)} off={busy} label="restore" name={`restore revision ${r.id}`} />
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
