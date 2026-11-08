import { useEffect, useRef, useState } from "react";
import { AdminHead } from "./App";
import { checkSession, createTimeline, hasText, login, logout as signOut } from "./adminApi";
import { Attachments, useAttachments } from "./attachments";
import { Button, Editor, EntryRow, type Send, sendOrExpire } from "./EntryEditor";
import type { Entry, Profile, Timeline } from "./timelineTypes";

// The private /admin page: a password form, then compose, edit and delete for timeline entries.
// It is lazy loaded, so public pages never download it, and nothing on the site links here.

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

  const send = sendOrExpire(() => expire());

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
          <Manager send={send} expire={expire} />
        </div>
      )}
    </article>
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

function Manager({ send, expire }: { send: Send; expire: () => void }) {
  const [entries, setEntries] = useState<Entry[]>([]);
  const [cursor, setCursor] = useState<number | null>(null);
  const [list, setList] = useState<"loading" | "ok" | "failed">("loading");
  const [more, setMore] = useState(false);
  const [listError, setListError] = useState("");
  // The poster and the first page's clock, which every entry here is shown with.
  const [shown, setShown] = useState<{ profile: Profile; now: string } | null>(null);
  const [draft, setDraft] = useState("");
  const [posting, setPosting] = useState(false);
  const [postError, setPostError] = useState("");
  const [status, setStatus] = useState("");
  const images = useAttachments(expire);
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
    const [r, p] = await Promise.all([page(null), send<Profile>("GET", "/api/profile")]);
    const failed = !r.ok ? r : !p.ok ? p : null;
    setListError(failed ? failed.message : "");
    setList(failed ? "failed" : "ok");
    if (r.ok && p.ok) setShown({ profile: p.data, now: r.data.now });
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
    if (inFlight.current || images.uploading) return;
    inFlight.current = true;
    setPosting(true);
    setPostError("");
    const r = await createTimeline(draft, images.payload());
    inFlight.current = false;
    setPosting(false);
    if (!r.ok) return r.status === 401 ? expire() : setPostError(r.message);
    setEntries((prev) => [r.data, ...prev]);
    setDraft("");
    images.clear();
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
        attachments={<Attachments state={images} busy={posting} id="compose" />}
      >
        <Button type="submit" busy={posting} off={!hasText(draft) || images.uploading} label="post" busyLabel="posting" />
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
        {shown &&
          entries.map((e) => (
            <EntryRow
              key={e.id}
              entry={e}
              profile={shown.profile}
              now={shown.now}
              send={send}
              onSaved={(saved, message) => {
                setEntries((prev) => prev.map((p) => (p.id === saved.id ? saved : p)));
                setStatus(message);
              }}
              onGone={remove}
              expire={expire}
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
