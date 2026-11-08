import { useEffect, useRef, useState } from "react";
import { Avatar } from "./App";
import { createTimeline, hasText } from "./adminApi";
import type { AdminPageProps } from "./adminPages";
import { Attachments, useAttachments } from "./attachments";
import { Button, Editor, EntryRow } from "./EntryEditor";
import type { Entry, Timeline } from "./timelineTypes";

// The timeline page: compose, then every entry with edit and delete.
export default function AdminTimeline({ send, expire, profile, loadProfile }: AdminPageProps) {
  const [entries, setEntries] = useState<Entry[]>([]);
  const [cursor, setCursor] = useState<number | null>(null);
  const [list, setList] = useState<"loading" | "ok" | "failed">("loading");
  const [more, setMore] = useState(false);
  const [listError, setListError] = useState("");
  // The first page's clock, which every entry here is shown with.
  const [now, setNow] = useState<string | null>(null);
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
    const [r, p] = await Promise.all([page(null), loadProfile()]);
    if (r.ok) setNow(r.data.now);
    const failed = [r, p].find((x) => !x.ok);
    if (failed && !failed.ok) {
      setListError(failed.message);
      setList("failed");
      return;
    }
    setListError("");
    setList("ok");
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
      <h1 tabIndex={-1}>timeline</h1>
      <section className="admin-section">
        <h2>new entry</h2>
        <div className="composer">
          {profile ? <Avatar profile={profile} /> : <div className="avatar" aria-hidden="true" />}
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
        </div>
      </section>
      <section className="admin-section entries">
        <h2>entries</h2>
        {list === "loading" && <p className="admin-state muted">loading</p>}
        {list === "failed" && (
          <div className="admin-state admin-row">
            <p className="error" role="alert">
              {listError}
            </p>
            <Button onClick={first} label="retry" />
          </div>
        )}
        {list === "ok" && entries.length === 0 && <p className="admin-state muted">nothing here yet</p>}
        <div className="timeline" ref={listRef}>
          {profile &&
            now &&
            entries.map((e) => (
              <EntryRow
                key={e.id}
                entry={e}
                profile={profile}
                now={now}
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
      </section>
    </>
  );
}
