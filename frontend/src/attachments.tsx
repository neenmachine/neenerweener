import { useEffect, useRef, useState } from "react";
import { type Attachment, uploadMedia } from "./adminApi";

// Images attached to a new timeline entry: upload on pick, a local preview, alt text, and the create payload.
// Admin only (lazy chunks). The server decides type and size; the client checks only the count.

export const MAX_IMAGES = 4;
type Item = { key: number; url: string; id: string | null; alt: string };

export function useAttachments(onExpired: () => void) {
  const [items, setItems] = useState<Item[]>([]);
  const [error, setError] = useState("");
  const nextKey = useRef(0);
  // Every live preview URL, so unmount can revoke the ones still shown.
  const urls = useRef(new Set<string>());
  useEffect(() => () => urls.current.forEach((url) => URL.revokeObjectURL(url)), []);

  const drop = (key: number) =>
    setItems((prev) =>
      prev.filter((item) => {
        if (item.key !== key) return true;
        URL.revokeObjectURL(item.url);
        urls.current.delete(item.url);
        return false;
      }),
    );

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
    added.forEach((item) => urls.current.add(item.url));
    setItems((prev) => [...prev, ...added]);
    added.forEach((item, i) => upload(item.key, files[i]));
  };

  const clear = () => {
    items.forEach((item) => drop(item.key));
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
      {state.items.map((item) => (
        <div key={item.key} className="attachment">
          <img src={item.url} alt="" />
          <label>
            alt text
            <input value={item.alt} readOnly={busy} onChange={(e) => state.setAlt(item.key, e.target.value)} />
          </label>
          <span className="admin-actions">
            {item.id === null && (
              <span className="muted" role="status">
                uploading
              </span>
            )}
            <button type="button" className="button" aria-disabled={busy} onClick={() => busy || state.remove(item.key)}>
              <span>remove</span>
            </button>
          </span>
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
        <span id={`${id}-files-error`} className="error" role="alert">
          {state.error}
        </span>
      </div>
    </div>
  );
}
