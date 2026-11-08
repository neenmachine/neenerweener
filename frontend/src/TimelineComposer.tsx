import { useRef, useState } from "react";
import { Avatar } from "./App";
import { createTimeline, hasText, LIMIT, length } from "./adminApi";
import { Attachments, useAttachments } from "./attachments";
import type { Entry, Profile } from "./timelineTypes";
import { useRequest } from "./useRequest";

// The admin's compose box at the top of /timeline. A lazy chunk: only loaded after the session check says signed in.
// The "admin" class reuses the /admin form styles; the poster's avatar sits left, as on the entries below.
export default function TimelineComposer({
  profile,
  onCreated,
  onExpired,
}: {
  profile: Profile;
  onCreated: (entry: Entry) => void;
  onExpired: () => void;
}) {
  const [text, setText] = useState("");
  const { run, busy } = useRequest("submit");
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");
  const textarea = useRef<HTMLTextAreaElement>(null);
  const images = useAttachments(onExpired);
  // An entry needs text; images go only once uploaded.
  const valid = hasText(text) && !images.uploading;
  const n = length(text);

  const post = async () => {
    if (!valid) return;
    const r = await run((signal) => {
      setError("");
      setStatus("");
      return createTimeline(text, images.payload(), signal);
    });
    if (!r.ok) return r.status === 401 ? onExpired() : setError(r.message);
    onCreated(r.data);
    setText("");
    images.clear();
    setStatus("posted");
    textarea.current?.focus();
  };

  return (
    <form
      className="admin editor composer"
      onSubmit={(e) => {
        e.preventDefault();
        post();
      }}
    >
      <Avatar profile={profile} />
      <div>
        <textarea
          ref={textarea}
          aria-label="new timeline entry"
          value={text}
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
        <Attachments state={images} busy={busy} id="compose" />
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
      </div>
    </form>
  );
}
