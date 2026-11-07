import { useRef, useState } from "react";
import { type Entry, createTimeline, LIMIT, length, validTimelineBody } from "./adminApi";

// The admin's compose box at the top of /timeline. A lazy chunk: only loaded after the session check says signed in.
// The "admin" class reuses the /admin form styles.
export default function TimelineComposer({ onCreated, onExpired }: { onCreated: (entry: Entry) => void; onExpired: () => void }) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");
  // A ref, not state: two submits in the same task both see busy=false.
  const inFlight = useRef(false);
  const textarea = useRef<HTMLTextAreaElement>(null);
  const valid = validTimelineBody(text);
  const n = length(text);

  const post = async () => {
    if (inFlight.current || !valid) return;
    inFlight.current = true;
    setBusy(true);
    setError("");
    setStatus("");
    const r = await createTimeline(text);
    inFlight.current = false;
    setBusy(false);
    if (!r.ok) return r.status === 401 ? onExpired() : setError(r.message);
    onCreated(r.data);
    setText("");
    setStatus("posted");
    textarea.current?.focus();
  };

  return (
    <form
      className="admin editor"
      onSubmit={(e) => {
        e.preventDefault();
        post();
      }}
    >
      <textarea
        ref={textarea}
        aria-label="new timeline entry"
        value={text}
        rows={5}
        // Keeps the text while the request runs, so a failure never loses it.
        readOnly={busy}
        aria-invalid={n > LIMIT || !!error || undefined}
        aria-describedby="compose-count compose-error"
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
            e.preventDefault();
            post();
          }
        }}
      />
      <div className="admin-row">
        <span id="compose-count" className={n > LIMIT ? "count error" : "count muted"}>
          {n.toLocaleString("en")} / {LIMIT.toLocaleString("en")}
        </span>
        {/* aria-disabled, not disabled, so keyboard focus stays on the button. */}
        <button
          type="submit"
          className="button"
          aria-disabled={busy || !valid}
          aria-busy={busy}
          onClick={busy || !valid ? (e) => e.preventDefault() : undefined}
        >
          <span>tweet</span>
        </button>
      </div>
      <p id="compose-error" className="error" role="alert">
        {error}
      </p>
      <p className="sr-only" role="status">
        {status}
      </p>
    </form>
  );
}
