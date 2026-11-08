import { useEffect, useRef, useState } from "react";
import { type Attachment, uploadMedia } from "./adminApi";
import type { Media } from "./timelineTypes";

// Images attached to a timeline entry: upload on pick, a local preview, alt text, and the create or edit payload.
// An edit starts from the entry's media, shown from the server; only new picks get blob previews.
// Admin only (lazy chunks). The server decides type and size; the client checks only the count.

export const MAX_IMAGES = 4;
type Item = { key: number; url: string; id: string | null; alt: string };

export function useAttachments(onExpired: () => void, initial: Media[] = []) {
  const [items, setItems] = useState<Item[]>(() =>
    initial.map((m, key) => ({ key, url: `/media/${m.id}/sm.webp`, id: m.id, alt: m.alt })),
  );
  const [error, setError] = useState("");
  const nextKey = useRef(initial.length);
  // The live preview URL of each row, revoked once here, never inside a state updater (StrictMode runs those twice).
  const urls = useRef(new Map<number, string>());
  const revoke = (keys: Iterable<number>) => {
    for (const key of [...keys]) {
      URL.revokeObjectURL(urls.current.get(key)!);
      urls.current.delete(key);
    }
  };
  // On unmount (sign-out). A full page load frees the document's blob URLs itself.
  useEffect(() => () => revoke(urls.current.keys()), []);

  const drop = (key: number) => {
    if (urls.current.has(key)) revoke([key]);
    setItems((prev) => prev.filter((item) => item.key !== key));
  };

  const upload = async (key: number, file: File) => {
    const r = await uploadMedia(file);
    if (!r.ok) {
      drop(key);
      return r.status === 401 ? onExpired() : setError(r.message);
    }
    // A row removed while uploading stays removed; the server sweeps the unattached image.
    setItems((prev) => prev.map((item) => (item.key === key ? { ...item, id: r.data.id } : item)));
  };

  const add = (files: File[]) => {
    if (!files.length) return;
    if (items.length + files.length > MAX_IMAGES) return setError(`up to ${MAX_IMAGES} images`);
    setError("");
    const added = files.map((file) => ({ key: nextKey.current++, url: URL.createObjectURL(file), id: null, alt: "" }));
    added.forEach((item) => urls.current.set(item.key, item.url));
    setItems((prev) => [...prev, ...added]);
    added.forEach((item, i) => upload(item.key, files[i]));
  };

  const clear = () => {
    revoke(urls.current.keys());
    setItems([]);
    setError("");
  };

  return {
    items,
    error,
    add,
    remove: drop,
    clear,
    setAlt: (key: number, alt: string) => setItems((prev) => prev.map((item) => (item.key === key ? { ...item, alt } : item))),
    uploading: items.some((item) => item.id === null),
    // In the order shown; an empty alt goes as "".
    payload: (): Attachment[] => items.map((item) => ({ id: item.id!, alt: item.alt })),
  };
}

export function Attachments({ state, busy, id }: { state: ReturnType<typeof useAttachments>; busy: boolean; id: string }) {
  return (
    <div className="attachments">
      {state.items.map((item, i) => (
        <div key={item.key} className="attachment">
          <img src={item.url} alt="" />
          <label>
            alt text
            <input value={item.alt} readOnly={busy} onChange={(e) => state.setAlt(item.key, e.target.value)} />
          </label>
          <button
            type="button"
            className="button"
            aria-label={`remove image ${i + 1}`}
            aria-disabled={busy}
            onClick={() => busy || state.remove(item.key)}
          >
            <span>remove</span>
          </button>
        </div>
      ))}
      <div className="admin-row">
        <input
          id={`${id}-files`}
          className="sr-only"
          type="file"
          accept="image/*"
          multiple
          disabled={busy}
          aria-describedby={`${id}-files-error`}
          onChange={(e) => {
            state.add([...(e.target.files ?? [])]);
            e.target.value = ""; // so picking the same file again fires change
          }}
        />
        <label htmlFor={`${id}-files`} className="button" aria-disabled={busy}>
          <span>attach images</span>
        </label>
        {/* Always mounted, so screen readers announce the change. */}
        <span id={`${id}-files-status`} className="muted" role="status">
          {state.uploading ? "uploading" : ""}
        </span>
        <span id={`${id}-files-error`} className="error" role="alert">
          {state.error}
        </span>
      </div>
    </div>
  );
}
