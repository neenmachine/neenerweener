import { type ReactNode, type Ref, type RefObject, useEffect, useRef, useState } from "react";
import { AdminHead, compact, full } from "./App";
import {
  type AdminEntry,
  type Result,
  checkSession,
  hasText,
  LIMIT,
  length,
  login,
  logout as signOut,
  request,
} from "./adminApi";
import type { Entry } from "./timelineTypes";

// The private /admin page: a password form, then compose, edit and delete for timeline entries.
// It is lazy loaded, so public pages never download it, and nothing on the site links here.

type Timeline = { entries: Entry[]; next_cursor: number | null };
type Send = <T = undefined>(method: string, url: string, body?: object) => Promise<Result<T>>;

export default function Admin() {
  const [auth, setAuth] = useState<"loading" | "unavailable" | "out" | "in">("loading");
  // The page is kept (hidden) while the session is out after a 401, so unsaved text survives logging in again.
  const [mounted, setMounted] = useState(false);
  const [notice, setNotice] = useState("");
  const [loggingOut, setLoggingOut] = useState(false);
  const outFlight = useRef(false);
  // The session check is usually fast; say "loading" only when it is not, so a fast check shows no flash.
  const [slow, setSlow] = useState(false);
  useEffect(() => {
    if (auth !== "loading") return;
    const t = setTimeout(() => setSlow(true), 400);
    return () => {
      clearTimeout(t);
      setSlow(false);
    };
  }, [auth]);

  const send: Send = async <T,>(method: string, url: string, body?: object) => {
    const r = await request<T>(method, url, body);
    if (!r.ok && r.status === 401) expire();
    return r;
  };

  const expire = () => {
    setNotice("session expired, log in again");
    setAuth("out");
  };

  // Returns whether the session is in, so login can tell a cookie that did not stick.
  const session = async () => {
    const r = await checkSession();
    if (!r.ok) {
      setNotice(r.message);
      setAuth("unavailable");
      return false;
    }
    setAuth(r.data.authenticated ? "in" : "out");
    if (r.data.authenticated) {
      setNotice("");
      setMounted(true);
    }
    return r.data.authenticated;
  };

  useEffect(() => {
    document.title = "admin";
    const robots = document.createElement("meta");
    robots.name = "robots";
    robots.content = "noindex";
    document.head.append(robots);
    session();
  }, []);

  const logout = async () => {
    if (outFlight.current) return;
    outFlight.current = true;
    setLoggingOut(true);
    const r = await signOut();
    outFlight.current = false;
    setLoggingOut(false);
    if (!r.ok && r.status !== 401) return setNotice(r.message);
    // A deliberate logout drops unsaved text with the page.
    setMounted(false);
    setNotice("");
    setAuth("out");
  };

  return (
    <article className="admin">
      <AdminHead>
        {auth === "in" && <Button onClick={logout} busy={loggingOut} label="log out" busyLabel="logging out" />}
      </AdminHead>
      {auth === "loading" && slow && (
        <p className="muted" role="status">
          loading
        </p>
      )}
      {auth === "unavailable" && (
        <div className="admin-row">
          <p className="error" role="alert">
            {notice}
          </p>
          <Button onClick={() => (setAuth("loading"), session())} label="retry" />
        </div>
      )}
      {auth === "out" && <Login session={session} notice={notice} />}
      {auth === "in" && (
        <p className="error" role="alert">
          {notice}
        </p>
      )}
      {mounted && (
        <div hidden={auth !== "in"}>
          <Manager send={send} />
        </div>
      )}
    </article>
  );
}

// A small outlined button; while busy it keeps focus and width and says busyLabel.
function Button({
  label,
  busyLabel = label,
  busy = false,
  off = false,
  type = "button",
  onClick,
  danger,
  ref,
  data,
}: {
  label: string;
  busyLabel?: string;
  busy?: boolean;
  off?: boolean;
  type?: "button" | "submit";
  onClick?: () => void;
  danger?: boolean;
  ref?: Ref<HTMLButtonElement>;
  data?: string;
}) {
  return (
    <button
      type={type}
      ref={ref}
      className={danger ? "button danger" : "button"}
      // aria-disabled, not disabled, so keyboard focus stays on the button.
      aria-disabled={busy || off}
      aria-busy={busy}
      onClick={busy || off ? (e) => e.preventDefault() : onClick}
      data-action={data}
    >
      <span hidden={busy}>{label}</span>
      <span hidden={!busy}>{busyLabel}</span>
    </button>
  );
}

function Login({ session, notice }: { session: () => Promise<boolean>; notice: string }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(notice);
  const [blockedFor, setBlockedFor] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  const inFlight = useRef(false);
  useEffect(() => {
    if (!blockedFor) return;
    const t = setTimeout(() => setBlockedFor(0), blockedFor * 1000);
    return () => clearTimeout(t);
  }, [blockedFor]);
  const submit = async () => {
    const field = input.current!;
    if (inFlight.current || blockedFor || !field.value) return;
    inFlight.current = true;
    setBusy(true);
    setError("");
    // Read from the field at submit time: the password never goes into React state.
    const r = await login(field.value);
    field.value = "";
    const ok = r.ok && (await session());
    inFlight.current = false;
    setBusy(false);
    if (ok) return;
    if (!r.ok && r.status === 429) setBlockedFor(r.retryAfter ?? 60);
    setError(!r.ok && r.status !== 401 ? r.message : "login failed");
    field.focus();
  };
  return (
    // method=post: if the script ever fails to stop the submit, the password still stays out of the URL.
    <form
      className="login"
      method="post"
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
    >
      <label htmlFor="password">password</label>
      <div className="admin-row">
        <input
          id="password"
          ref={input}
          type="password"
          name="password"
          autoComplete="current-password"
          autoFocus
          required
          aria-invalid={error === "login failed" || undefined}
          aria-describedby="login-error"
        />
        <Button type="submit" busy={busy} off={!!blockedFor} label="log in" busyLabel="logging in" />
      </div>
      <p id="login-error" className="error" role="alert">
        {error}
      </p>
    </form>
  );
}

function Manager({ send }: { send: Send }) {
  const [entries, setEntries] = useState<Entry[]>([]);
  const [cursor, setCursor] = useState<number | null>(null);
  const [list, setList] = useState<"loading" | "ok" | "failed">("loading");
  const [more, setMore] = useState(false);
  const [listError, setListError] = useState("");
  const [draft, setDraft] = useState("");
  const [posting, setPosting] = useState(false);
  const [postError, setPostError] = useState("");
  const [status, setStatus] = useState("");
  const inFlight = useRef(false);
  const moreFlight = useRef(false);
  const listRef = useRef<HTMLDivElement>(null);
  const compose = useRef<HTMLTextAreaElement>(null);
  // Index of the entry whose edit button gets focus after a delete removes the focused one.
  const focusAt = useRef<number | null>(null);

  const page = async (before: number | null) => {
    const r = await send<Timeline>("GET", before ? `/api/timeline?before=${before}` : "/api/timeline");
    if (!r.ok) return r;
    setEntries((prev) => [...prev, ...r.data.entries.filter((e) => !prev.some((p) => p.id === e.id))]);
    setCursor(r.data.next_cursor);
    return r;
  };

  const first = async () => {
    setList("loading");
    const r = await page(null);
    setListError(r.ok ? "" : r.message);
    setList(r.ok ? "ok" : "failed");
  };

  useEffect(() => {
    first();
  }, []);

  useEffect(() => {
    if (focusAt.current === null) return;
    const buttons = listRef.current?.querySelectorAll<HTMLElement>("[data-action=edit]");
    (buttons?.[Math.min(focusAt.current, buttons.length - 1)] ?? compose.current)?.focus();
    focusAt.current = null;
  }, [entries]);

  const loadMore = async () => {
    if (moreFlight.current || cursor === null) return;
    moreFlight.current = true;
    setMore(true);
    setStatus("loading");
    const r = await page(cursor);
    moreFlight.current = false;
    setMore(false);
    setListError(r.ok ? "" : r.message);
    setStatus(r.ok ? `${r.data.entries.length} more loaded` : r.message);
  };

  const post = async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    setPosting(true);
    setPostError("");
    const r = await send<AdminEntry>("POST", "/api/admin/timeline", { body_markdown: draft });
    inFlight.current = false;
    setPosting(false);
    if (!r.ok) return setPostError(r.message);
    setEntries((prev) => [r.data, ...prev]);
    setDraft("");
    setStatus("posted");
    compose.current?.focus();
  };

  const remove = (id: number, message: string) => {
    const index = entries.findIndex((e) => e.id === id);
    if (listRef.current?.contains(document.activeElement)) focusAt.current = index;
    setEntries((prev) => prev.filter((e) => e.id !== id));
    setStatus(message);
  };

  return (
    <>
      <Editor
        id="compose"
        name="new entry"
        text={draft}
        setText={setDraft}
        textarea={compose}
        busy={posting}
        error={postError}
        onSubmit={post}
      >
        <Button type="submit" busy={posting} off={!hasText(draft)} label="post" busyLabel="posting" />
      </Editor>
      {list === "loading" && <p className="muted">loading</p>}
      {list === "failed" && (
        <div className="admin-row">
          <p className="error" role="alert">
            {listError}
          </p>
          <Button onClick={first} label="retry" />
        </div>
      )}
      <div className="timeline" ref={listRef}>
        {entries.map((e) => (
          <Row
            key={e.id}
            entry={e}
            send={send}
            onSaved={(saved) => {
              setEntries((prev) => prev.map((p) => (p.id === saved.id ? saved : p)));
              setStatus("saved");
            }}
            onGone={remove}
          />
        ))}
      </div>
      {list === "ok" && cursor !== null && (
        <div className="admin-row load-more">
          <Button onClick={loadMore} busy={more} label="load more" busyLabel="loading" />
          {listError && (
            <span className="error" aria-hidden="true">
              {listError}
            </span>
          )}
        </div>
      )}
      <p className="sr-only" role="status">
        {status}
      </p>
    </>
  );
}

// Markdown text, a code point counter, the action buttons and the error for one form.
function Editor({
  id,
  name,
  text,
  setText,
  textarea,
  busy,
  error,
  onSubmit,
  onCancel,
  children,
}: {
  id: string;
  name: string;
  text: string;
  setText: (text: string) => void;
  textarea?: Ref<HTMLTextAreaElement>;
  busy: boolean;
  error: string;
  onSubmit: () => void;
  onCancel?: () => void;
  children: ReactNode;
}) {
  const n = length(text);
  const submit = () => !busy && hasText(text) && onSubmit();
  return (
    <form
      className="editor"
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
    >
      <textarea
        ref={textarea}
        aria-label={name}
        value={text}
        rows={5}
        // Keeps the text while a request runs, so a failure never loses it.
        readOnly={busy}
        // The compose box after login, the edit box when it opens.
        autoFocus
        aria-invalid={n > LIMIT || !!error || undefined}
        aria-describedby={`${id}-count ${id}-error`}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
            e.preventDefault();
            submit();
          }
        }}
      />
      <div className="admin-row">
        <span id={`${id}-count`} className={n > LIMIT ? "count error" : "count muted"}>
          {n.toLocaleString("en")} / {LIMIT.toLocaleString("en")}
        </span>
        <span className="admin-actions">
          {onCancel && <Button onClick={onCancel} off={busy} label="cancel" />}
          {children}
        </span>
      </div>
      <p id={`${id}-error`} className="error" role="alert">
        {error}
      </p>
    </form>
  );
}

function Row({
  entry,
  send,
  onSaved,
  onGone,
}: {
  entry: Entry;
  send: Send;
  onSaved: (entry: Entry) => void;
  onGone: (id: number, message: string) => void;
}) {
  const [mode, setMode] = useState<"view" | "opening" | "edit" | "confirm" | "deleting">("view");
  const [text, setText] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const inFlight = useRef(false);
  const editButton = useRef<HTMLButtonElement>(null);
  const deleteButton = useRef<HTMLButtonElement>(null);
  const cancelDelete = useRef<HTMLButtonElement>(null);
  // Which button gets focus once the view buttons are back; they do not exist while editing.
  const refocus = useRef<RefObject<HTMLButtonElement | null>>(null);
  useEffect(() => {
    if (mode === "confirm") cancelDelete.current?.focus();
    if (mode === "view") refocus.current?.current?.focus();
    refocus.current = null;
  }, [mode]);

  // Runs one request at a time for this entry; a 404 means another tab or device removed it.
  const run = async <T,>(request: () => Promise<Result<T>>, done: (data: T) => void) => {
    if (inFlight.current) return;
    inFlight.current = true;
    setError("");
    const r = await request();
    inFlight.current = false;
    if (r.ok) return done(r.data);
    if (r.status === 404) return onGone(entry.id, "entry not found");
    setError(r.message);
    return false;
  };

  const open = () => {
    setMode("opening");
    run(
      () => send<AdminEntry>("GET", `/api/admin/timeline/${entry.id}`),
      (data) => {
        setText(data.body_markdown);
        setMode("edit");
      },
    ).then((r) => r === false && setMode("view"));
  };

  const save = () => {
    setSaving(true);
    run(
      () => send<AdminEntry>("PATCH", `/api/admin/timeline/${entry.id}`, { body_markdown: text }),
      (data) => {
        onSaved(data);
        refocus.current = editButton;
        setMode("view");
      },
    ).finally(() => setSaving(false));
  };

  const cancel = (button: RefObject<HTMLButtonElement | null>) => () => {
    setError("");
    refocus.current = button;
    setMode("view");
  };

  const remove = () => {
    setMode("deleting");
    run(
      () => send("DELETE", `/api/admin/timeline/${entry.id}`),
      () => onGone(entry.id, "deleted"),
    ).then((r) => r === false && setMode("confirm"));
  };

  return (
    <article className="timeline-entry" id={`entry-${entry.id}`}>
      {mode === "edit" ? (
        <Editor
          id={`edit-${entry.id}`}
          name="edit entry"
          text={text}
          setText={setText}
          busy={saving}
          error={error}
          onSubmit={save}
          onCancel={cancel(editButton)}
        >
          <Button type="submit" busy={saving} off={!hasText(text)} label="save" busyLabel="saving" />
        </Editor>
      ) : (
        // Rendered server side from Henry's entry, with raw HTML disabled.
        <div dangerouslySetInnerHTML={{ __html: entry.html }} />
      )}
      <footer>
        <a href={`/timeline#entry-${entry.id}`} aria-label={full(entry.created_at)}>
          <time dateTime={new Date(entry.created_at).toISOString()} title={full(entry.created_at)}>
            {compact(entry.created_at)}
          </time>
        </a>
        {entry.edited_at && (
          <span className="edited" title={full(entry.edited_at)}>
            edited<span className="sr-only"> {full(entry.edited_at)}</span>
          </span>
        )}
        {(mode === "view" || mode === "opening") && (
          <span className="admin-actions">
            <Button ref={editButton} onClick={open} busy={mode === "opening"} label="edit" busyLabel="loading" data="edit" />
            <Button ref={deleteButton} onClick={() => setMode("confirm")} off={mode === "opening"} label="delete" />
          </span>
        )}
        {(mode === "confirm" || mode === "deleting") && (
          <span
            className="admin-actions"
            role="group"
            aria-label="delete this entry?"
            onKeyDown={(e) => e.key === "Escape" && mode === "confirm" && cancel(deleteButton)()}
          >
            <span aria-hidden="true">delete this entry?</span>
            <Button onClick={remove} busy={mode === "deleting"} label="delete" busyLabel="deleting" danger />
            <Button ref={cancelDelete} onClick={cancel(deleteButton)} off={mode === "deleting"} label="cancel" />
          </span>
        )}
      </footer>
      <p className="error" role="alert">
        {mode !== "edit" && error}
      </p>
    </article>
  );
}
