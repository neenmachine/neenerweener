import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { compact } from "./App";
import { hasText, type Result, type Uploaded } from "./adminApi";
import type { AdminPageProps } from "./adminPages";
import { Button, type Send } from "./EntryEditor";
import { imageFiles, imageMarkdown, insertAt, type Spot } from "./postPaste";
import { type Preview as Rendered, previewDocument } from "./postPreview";
import type { Control, FrameEdit, FrameImage, FrameStart } from "./previewFrame";
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

// The server's slug rule (backend/posts.py SLUG); it also rejects reserved slugs.
const SLUG = /^[a-z0-9-]{1,100}$/;
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
  const markdown = useRef<HTMLTextAreaElement>(null);
  const [uploading, setUploading] = useState(0);
  const [uploadError, setUploadError] = useState("");
  // Uploads end with the editor: an image that answers after it closed never inserts anything.
  const life = useRef<AbortController>(null!);
  useEffect(() => {
    life.current = new AbortController();
    return () => life.current.abort();
  }, []);
  const caret = useRef<number | null>(null);
  useLayoutEffect(() => {
    const el = markdown.current;
    if (caret.current === null || !el) return;
    // Back to the textarea after the images, unless Henry has moved on to another field meanwhile.
    if (!el.hidden && (document.activeElement === el || document.activeElement === document.body)) {
      el.focus();
      el.setSelectionRange(caret.current, caret.current);
    }
    caret.current = null;
  }, [fields.body]);

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

  const set = (key: keyof Fields) => (e: { target: { value: string } }) => {
    setFields((f) => ({ ...f, [key]: e.target.value }));
    setStatus("");
  };

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
  const reload = () => call((signal) => send<Post>("GET", `/api/admin/posts/${post!.id}`, undefined, signal), "");
  const back = () => (!dirty || confirm(DISCARD)) && onClose();

  // Pasted or dropped images: each uploads through the media pipeline, then all that succeeded go in, in order, where
  // the text was when they came. A failed one shows its error and the text stays as it is.
  const addImages = async (files: File[], el: HTMLTextAreaElement) => {
    const spot: Spot = { text: el.value, start: el.selectionStart, end: el.selectionEnd };
    const { signal } = life.current;
    setUploading((n) => n + 1);
    setUploadError("");
    const results = await Promise.all(files.map((file) => send<Uploaded>("POST", "/api/admin/media", file, signal)));
    if (signal.aborted) return;
    setUploading((n) => n - 1);
    const failed = results.find((r) => !r.ok);
    if (failed && !failed.ok && failed.status !== 401) setUploadError(failed.message);
    const ids = results.flatMap((r) => (r.ok ? [r.data.id] : []));
    if (!ids.length || !markdown.current) return;
    const done = insertAt(markdown.current.value, spot, ids.map(imageMarkdown).join("\n\n"));
    caret.current = done.caret;
    setFields((f) => ({ ...f, body: done.text }));
    setStatus("");
  };
  const takeImages = (e: React.ClipboardEvent<HTMLTextAreaElement> | React.DragEvent<HTMLTextAreaElement>) => {
    const data = "clipboardData" in e ? e.clipboardData : e.dataTransfer;
    const files = imageFiles(data);
    // A dropped file that is not an image is refused too, so the browser does not open it in place of the editor.
    if (files.length || (e.type === "drop" && data?.types.includes("Files"))) e.preventDefault();
    if (files.length && !busy && !deleted) addImages(files, e.currentTarget);
  };

  // Not while an image uploads: the saved post would replace the text before its markdown goes in.
  const canSave = uploading === 0 && !deleted && hasText(fields.title) && SLUG.test(fields.slug);
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
          if (canSave && (!live || hasText(fields.body))) save(live ? "live" : "draft", "saved");
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
            <span className="muted" role="status">
              {uploading > 0 ? "uploading" : ""}
            </span>
          </div>
          {/* Kept mounted while previewing, so its text, selection and undo history stay. */}
          <textarea
            className="markdown"
            aria-labelledby="post-markdown-label"
            hidden={mode !== "raw"}
            ref={markdown}
            value={fields.body}
            onChange={set("body")}
            onPaste={takeImages}
            onDragOver={(e) => e.dataTransfer.types.includes("Files") && e.preventDefault()}
            onDrop={takeImages}
            readOnly={busy || deleted}
          />
          <Preview body={fields.body} shown={mode === "preview"} send={send} onEdit={(body) => setFields((f) => ({ ...f, body }))} />
          {uploadError && (
            <p className="error" role="alert">
              {uploadError}
            </p>
          )}
        </div>
        <div className="save-row">
          <div className="admin-row">
            {!deleted && !confirming && (
              <span className="admin-actions">
                {live ? (
                  <>
                    <Button type="submit" busy={busy} off={!canSave || !hasText(fields.body)} label="save" busyLabel="saving" />
                    <Button onClick={() => save("draft", "unpublished")} busy={busy} off={!canSave} label="unpublish" busyLabel="saving" />
                  </>
                ) : (
                  <>
                    <Button type="submit" busy={busy} off={!canSave} label="save draft" busyLabel="saving" />
                    <Button onClick={() => save("live", "published")} busy={busy} off={!canSave || !hasText(fields.body)} label="publish" busyLabel="saving" />
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
                onKeyDown={(e) => e.key === "Escape" && !busy && (setConfirming(false), setTimeout(() => deleteButton.current?.focus()))}
              >
                <Button ref={confirmButton} onClick={remove} busy={busy} label="confirm delete" busyLabel="deleting" danger />
                <Button onClick={() => setConfirming(false)} off={busy} label="cancel" />
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
// older one. The frame may ask to move or resize one managed image; the server rewrites that image's source and
// answers with the new markdown and its rendering, which onEdit gives back to the editor.
function Preview({ body, shown, send, onEdit }: { body: string; shown: boolean; send: Send; onEdit: (body: string) => void }) {
  const { run, busy } = useRequest("load");
  const [doc, setDoc] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");
  const rendered = useRef<string | null>(null);
  // The shown document's channel token and images: a frame message must name both.
  const current = useRef<{ token: string; images: FrameImage[]; blocks: number } | null>(null);
  const frame = useRef<HTMLIFrameElement>(null);
  const wasShown = useRef(false);

  type Edit = { media_id: string; move_to?: number; width?: number };
  const render = async (text: string, edit?: Edit, restore: Pick<FrameStart, "scroll" | "focus"> = { scroll: 0, focus: null }) => {
    const r = await run(async (signal) => {
      setError("");
      setStatus("");
      const p = await send<Rendered & { body_markdown?: string }>("POST", "/api/admin/posts/preview", { body_markdown: text, ...edit }, signal);
      return p.ok ? { ok: true as const, p: p.data, ...(await previewDocument(p.data, restore)) } : p;
    });
    if (!r.ok) return r.status !== 401 && setError(r.message);
    const next = r.p.body_markdown ?? text;
    rendered.current = next;
    current.current = { token: r.token, images: r.p.images, blocks: r.p.blocks };
    setDoc(r.doc);
    if (edit) {
      onEdit(next);
      const moved = r.p.images.find((i) => i.id === edit.media_id);
      if (moved) setStatus(edit.width !== undefined ? `width ${edit.width} px` : `position ${moved.block + 1} of ${r.p.blocks}`);
    }
  };
  useEffect(() => {
    const opening = !wasShown.current;
    wasShown.current = shown;
    if (!shown || rendered.current === body) return;
    const t = setTimeout(() => render(body), opening ? 0 : 400);
    return () => clearTimeout(t);
  }, [shown, body]);

  // A frame message is a request, never trusted: only from this frame's window, with this rendering's token, for one
  // of its images, one valid action, and only while the text is still the one rendered.
  useEffect(() => {
    const listen = (e: MessageEvent) => {
      const c = current.current;
      const m = e.data as Partial<FrameEdit> | null;
      if (!c || e.source !== frame.current?.contentWindow || typeof m !== "object" || m === null || m.token !== c.token) return;
      if (!c.images.some((i) => i.id === m.id) || rendered.current !== body) return;
      const move = Number.isInteger(m.move_to) && m.move_to! >= 0 && m.move_to! <= c.blocks;
      const width = Number.isInteger(m.width) && m.width! >= 1;
      if (move === width) return;
      const control = CONTROLS.includes(m.control as Control) ? (m.control as Control) : null;
      const scroll = typeof m.scroll === "number" && Number.isFinite(m.scroll) ? m.scroll : 0;
      current.current = null; // one edit per rendering
      render(body, move ? { media_id: m.id!, move_to: m.move_to } : { media_id: m.id!, width: m.width }, {
        scroll,
        focus: control && { id: m.id!, control },
      });
    };
    addEventListener("message", listen);
    return () => removeEventListener("message", listen);
  }, [body]);

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
      {/* allow-scripts without allow-same-origin: an opaque origin; the document's own CSP lets only the controller run. */}
      {doc !== null && <iframe ref={frame} className="post-preview" sandbox="allow-scripts" srcDoc={doc} title="preview" />}
      <span className="sr-only" role="status">
        {status}
      </span>
    </div>
  );
}

const CONTROLS: Control[] = ["earlier", "later", "smaller", "larger", "width"];

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
