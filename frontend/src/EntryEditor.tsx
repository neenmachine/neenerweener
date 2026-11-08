import { type ReactNode, type Ref, type RefObject, useEffect, useRef, useState } from "react";
import { TimelineEntry } from "./App";
import { type AdminEntry, type Result, hasText, LIMIT, length, request } from "./adminApi";
import { Attachments, useAttachments } from "./attachments";
import type { Entry, Profile } from "./timelineTypes";

// The admin-only entry controls shared by /admin and /timeline: edit in place and inline delete confirm.
// Lazy loaded with the admin code, so public pages never download it.

export type Send = <T = undefined>(method: string, url: string, body?: object) => Promise<Result<T>>;

// An authenticated request; a 401 (the session ended) also calls expire.
export const sendOrExpire =
  (expire: () => void): Send =>
  async <T,>(method: string, url: string, body?: object) => {
    const r = await request<T>(method, url, body);
    if (!r.ok && r.status === 401) expire();
    return r;
  };

// A small outlined button; while busy it keeps focus and width and says busyLabel. name, if set, is the
// accessible name and starts with the visible label.
export function Button({
  label,
  busyLabel = label,
  busy = false,
  off = false,
  type = "button",
  onClick,
  danger,
  ref,
  data,
  name,
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
  name?: string;
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
      aria-label={name}
    >
      <span hidden={busy}>{label}</span>
      <span hidden={!busy}>{busyLabel}</span>
    </button>
  );
}

// Markdown text, a code point counter, the action buttons and the error for one form.
export function Editor({
  id,
  name,
  text,
  setText,
  textarea,
  busy,
  error,
  onSubmit,
  onCancel,
  cancelName,
  escapeOff = false,
  attachments,
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
  cancelName?: string;
  escapeOff?: boolean;
  attachments?: ReactNode;
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
      onKeyDown={(e) => e.key === "Escape" && onCancel && !busy && !escapeOff && onCancel()}
    >
      <textarea
        ref={textarea}
        aria-label={name}
        value={text}
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
      {attachments}
      <div className="admin-row">
        <span id={`${id}-count`} className={n > LIMIT ? "count error" : "count muted"}>
          {n.toLocaleString("en")} / {LIMIT.toLocaleString("en")}
        </span>
        <span className="admin-actions">
          {onCancel && <Button onClick={onCancel} off={busy} label="cancel" name={cancelName} />}
          {children}
        </span>
      </div>
      <p id={`${id}-error`} className="error" role="alert">
        {error}
      </p>
    </form>
  );
}

// className and href are the entry's, as the page renders it (/timeline adds "target" and links in page).
export function EntryRow({
  entry,
  profile,
  now,
  className,
  href = `/timeline#entry-${entry.id}`,
  send,
  onSaved,
  onGone,
  expire,
}: {
  entry: Entry;
  profile: Profile;
  now: string;
  className?: string;
  href?: string;
  send: Send;
  onSaved: (entry: Entry, message: string) => void;
  onGone: (id: number, message: string) => void;
  expire: () => void;
}) {
  const [mode, setMode] = useState<"view" | "opening" | "edit" | "confirm" | "deleting">("view");
  // The private entry from GET, the start of the edit form.
  const [opened, setOpened] = useState<AdminEntry | null>(null);
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
        setOpened(data);
        setMode("edit");
      },
    ).then((r) => r === false && setMode("view"));
  };

  const save = (edit: object) => {
    setSaving(true);
    run(
      () => send<AdminEntry>("PATCH", `/api/admin/timeline/${entry.id}`, edit),
      (data) => {
        onSaved(data, "saved");
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
    <TimelineEntry
      entry={entry}
      profile={profile}
      now={now}
      className={className}
      href={href}
      body={
        mode === "edit" && opened ? (
          <EditForm
            entry={opened}
            expire={expire}
            saving={saving}
            error={error}
            onSave={save}
            onCancel={cancel(editButton)}
          />
        ) : undefined
      }
      actions={
        <>
          {(mode === "view" || mode === "opening") && (
            <span className="admin-actions">
              <Button
                ref={editButton}
                onClick={open}
                busy={mode === "opening"}
                label="edit"
                busyLabel="loading"
                data="edit"
                name={`edit entry ${entry.id}`}
              />
              <Button
                ref={deleteButton}
                onClick={() => setMode("confirm")}
                off={mode === "opening"}
                label="delete"
                name={`delete entry ${entry.id}`}
              />
            </span>
          )}
          {(mode === "confirm" || mode === "deleting") && (
            <span
              className="admin-actions"
              role="group"
              aria-label={`delete entry ${entry.id}?`}
              onKeyDown={(e) => e.key === "Escape" && mode === "confirm" && cancel(deleteButton)()}
            >
              <Button
                onClick={remove}
                busy={mode === "deleting"}
                label="confirm delete"
                busyLabel="deleting"
                danger
                name={`confirm delete entry ${entry.id}`}
              />
              <Button
                ref={cancelDelete}
                onClick={cancel(deleteButton)}
                off={mode === "deleting"}
                label="cancel"
                name={`cancel delete entry ${entry.id}`}
              />
            </span>
          )}
        </>
      }
    >
      <p className="error" role="alert">
        {mode !== "edit" && error}
      </p>
    </TimelineEntry>
  );
}

// The open edit: text plus the full image list (kept, removed, added), sent as one PATCH replacement.
function EditForm({
  entry,
  expire,
  saving,
  error,
  onSave,
  onCancel,
}: {
  entry: AdminEntry;
  expire: () => void;
  saving: boolean;
  error: string;
  onSave: (edit: object) => void;
  onCancel: () => void;
}) {
  const [text, setText] = useState(entry.body_markdown);
  const images = useAttachments(expire, entry.media);
  const id = `edit-${entry.id}`;
  return (
    <Editor
      id={id}
      name={`edit entry ${entry.id}`}
      text={text}
      setText={setText}
      busy={saving}
      error={error}
      onSubmit={() => images.uploading || onSave({ body_markdown: text, attachments: images.payload() })}
      onCancel={onCancel}
      cancelName={`cancel edit entry ${entry.id}`}
      escapeOff={images.uploading}
      attachments={<Attachments state={images} busy={saving} id={id} />}
    >
      <Button
        type="submit"
        busy={saving}
        off={!hasText(text) || images.uploading}
        label="save"
        busyLabel="saving"
        name={`save entry ${entry.id}`}
      />
    </Editor>
  );
}
