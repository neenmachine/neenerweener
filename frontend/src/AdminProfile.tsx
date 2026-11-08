import { type ChangeEvent, type FormEvent, useEffect, useRef, useState } from "react";
import { Avatar } from "./App";
import { hasText, length } from "./adminApi";
import type { AdminPageProps } from "./adminPages";
import { Button, type Send } from "./EntryEditor";
import type { Profile } from "./timelineTypes";

// The poster shown on every entry: picture, display name and handle.
export default function AdminProfile({ send, profile, loadProfile, setProfile }: AdminPageProps) {
  const [state, setState] = useState<"loading" | "ok" | "failed">(profile ? "ok" : "loading");
  const [error, setError] = useState("");

  const load = async () => {
    setState("loading");
    const r = await loadProfile();
    setError(r.ok ? "" : r.message);
    setState(r.ok ? "ok" : "failed");
  };

  useEffect(() => {
    if (!profile) load();
  }, []);

  return (
    <>
      <h1 tabIndex={-1}>profile</h1>
      {state === "loading" && <p className="admin-state muted">loading</p>}
      {state === "failed" && (
        <div className="admin-state admin-row">
          <p className="error" role="alert">
            {error}
          </p>
          <Button onClick={load} label="retry" />
        </div>
      )}
      {state === "ok" && profile && <ProfileForm initial={profile} send={send} setProfile={setProfile} />}
    </>
  );
}

// The server's rules (backend/timeline.py update_profile), used only to pick the field to focus after its 422.
const HANDLE = /^[A-Za-z0-9_]{1,30}$/;
const firstInvalid = (name: string, handle: string) =>
  !hasText(name) || length(name.trim()) > 80 ? "name" : handle && !HANDLE.test(handle) ? "handle" : null;

// A picked picture uploads at once (staged on the server), but it, a removal and the fields change the profile
// only on save profile. undefined: keep the saved picture; null: remove it; otherwise the staged upload.
type Staged = undefined | null | { id: string; url: string };

function ProfileForm({ initial, send, setProfile }: { initial: Profile; send: Send; setProfile: (p: Profile) => void }) {
  const [saved, setSaved] = useState(initial);
  const [name, setName] = useState(initial.display_name);
  const [handle, setHandle] = useState(initial.handle ?? "");
  const [staged, setStaged] = useState<Staged>(undefined);
  const [uploading, setUploading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [invalid, setInvalid] = useState<"name" | "handle" | null>(null);
  const [status, setStatus] = useState("");
  const file = useRef<HTMLInputElement>(null);
  const pick = useRef<HTMLButtonElement>(null);
  const nameField = useRef<HTMLInputElement>(null);
  const handleField = useRef<HTMLInputElement>(null);

  // The staged picture's local preview URL, revoked when it is replaced, saved, or the form goes (sign-out).
  const preview = useRef<string | null>(null);
  const stage = (next: Staged) => {
    if (preview.current) URL.revokeObjectURL(preview.current);
    preview.current = next ? next.url : null;
    setStaged(next);
  };
  useEffect(() => () => void (preview.current && URL.revokeObjectURL(preview.current)), []);

  const edited = () => {
    setStatus("");
    setInvalid(null);
  };

  const upload = async (e: ChangeEvent<HTMLInputElement>) => {
    const picked = e.target.files?.[0];
    e.target.value = ""; // so picking the same file again fires change
    if (!picked) return;
    setUploading(true);
    setError("");
    setStatus("");
    const r = await send<{ id: string }>("POST", "/api/admin/profile/avatar", picked);
    setUploading(false);
    if (r.ok) return stage({ id: r.data.id, url: URL.createObjectURL(picked) });
    if (r.status === 401) return; // the shell asks to log in again; the form stays as it is
    setError(r.message);
    pick.current?.focus();
  };

  const remove = () => {
    stage(null);
    setStatus("");
    pick.current?.focus(); // the remove button is gone
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault(); // a busy or off save button already stops its own submit
    setSaving(true);
    setError("");
    setStatus("");
    setInvalid(null);
    const r = await send<Profile>("PATCH", "/api/admin/profile", {
      display_name: name,
      handle: handle || null,
      ...(staged !== undefined && { avatar_id: staged && staged.id }),
    });
    setSaving(false);
    if (!r.ok) {
      if (r.status === 401) return;
      setError(r.message);
      if (r.status !== 422) return;
      const field = firstInvalid(name, handle);
      setInvalid(field);
      (field === "name" ? nameField : field === "handle" ? handleField : pick).current?.focus();
      return;
    }
    stage(undefined);
    setSaved(r.data);
    setName(r.data.display_name);
    setHandle(r.data.handle ?? "");
    setProfile(r.data);
    setStatus("profile saved");
  };

  const hasPicture = staged === undefined ? !!saved.avatar_id : staged !== null;
  return (
    <form className="admin-section profile" onSubmit={submit}>
      <div className="picture" role="group" aria-label="profile picture">
        {staged ? (
          <img className="avatar preview" src={staged.url} alt="" aria-hidden="true" />
        ) : (
          <Avatar profile={staged === null ? { ...saved, avatar_id: null } : saved} size={88} />
        )}
        <input ref={file} type="file" accept="image/*" hidden onChange={upload} />
        <div className="picture-buttons">
          <Button
            ref={pick}
            onClick={() => file.current?.click()}
            busy={uploading}
            off={saving}
            label={hasPicture ? "replace picture" : "choose picture"}
            busyLabel="uploading"
          />
          {hasPicture && <Button onClick={remove} off={uploading || saving} label="remove picture" />}
        </div>
      </div>
      <div className="fields">
        <div>
          <label htmlFor="display-name">display name</label>
          <input
            id="display-name"
            ref={nameField}
            value={name}
            autoComplete="off"
            aria-invalid={invalid === "name" || undefined}
            aria-describedby="profile-error"
            onChange={(e) => (setName(e.target.value), edited())}
          />
        </div>
        <div>
          <label htmlFor="handle">handle</label>
          {/* The @ is shown inside the border but is not part of the value; the wrapper draws the border. */}
          <div
            className="handle-field"
            onMouseDown={(e) => e.target !== handleField.current && (e.preventDefault(), handleField.current?.focus())}
          >
            <span aria-hidden="true">@</span>
            <input
              id="handle"
              ref={handleField}
              value={handle}
              autoComplete="off"
              autoCapitalize="none"
              spellCheck={false}
              aria-invalid={invalid === "handle" || undefined}
              aria-describedby="profile-error"
              onChange={(e) => (setHandle(e.target.value), edited())}
            />
          </div>
        </div>
        <div className="save-row">
          <div className="admin-row">
            <Button type="submit" busy={saving} off={uploading} label="save profile" busyLabel="saving" />
            <p className="muted" role="status">
              {status}
            </p>
          </div>
          <p id="profile-error" className="error" role="alert">
            {error}
          </p>
        </div>
      </div>
    </form>
  );
}
