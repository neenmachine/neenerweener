import { type ChangeEvent, type FormEvent, useEffect, useRef, useState } from "react";
import { Avatar } from "./App";
import { hasText, length } from "./adminApi";
import type { AdminPageProps } from "./adminPages";
import { Button, type Send } from "./EntryEditor";
import type { Profile } from "./timelineTypes";
import { useRequest } from "./useRequest";

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
      {state === "ok" && profile && <ProfileForm profile={profile} send={send} setProfile={setProfile} />}
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

function ProfileForm({ profile, send, setProfile }: { profile: Profile; send: Send; setProfile: (p: Profile) => void }) {
  const [saved, setSaved] = useState(profile);
  const [name, setName] = useState(profile.display_name);
  const [handle, setHandle] = useState(profile.handle ?? "");
  const [staged, setStaged] = useState<Staged>(undefined);
  // A field the user typed in since the last save, even back to the saved text.
  const [nameDirty, setNameDirty] = useState(false);
  const [handleDirty, setHandleDirty] = useState(false);
  // A profile saved in another tab: an untouched field takes the new value; typed text stays, and so does a staged
  // picture.
  useEffect(() => {
    if (profile === saved) return;
    if (!nameDirty) setName(profile.display_name);
    if (!handleDirty) setHandle(profile.handle ?? "");
    setSaved(profile);
  }, [profile]);
  // A new pick supersedes an upload still running; one save at a time (Enter in a field submits without the button).
  const { run: uploadRun, busy: uploading } = useRequest("load");
  const { run: saveRun, busy: saving } = useRequest("submit");
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
  // An upload that answers after the form goes (sign-out) never settles, so it makes no preview URL nobody revokes.
  useEffect(
    () => () => {
      if (preview.current) URL.revokeObjectURL(preview.current);
    },
    [],
  );

  const edited = () => {
    setStatus("");
    setInvalid(null);
  };

  // The active upload, settling to the staged picture or false on failure, so a save submitted meanwhile waits for it.
  const pending = useRef<Promise<Staged | false> | null>(null);
  const upload = (e: ChangeEvent<HTMLInputElement>) => {
    const picked = e.target.files?.[0];
    e.target.value = ""; // so picking the same file again fires change
    if (!picked) return;
    const job = (pending.current = uploadFile(picked));
    job.then(() => pending.current === job && (pending.current = null));
  };
  const uploadFile = async (picked: File): Promise<Staged | false> => {
    const r = await uploadRun((signal) => {
      setError("");
      setStatus("");
      return send<{ id: string }>("POST", "/api/admin/profile/avatar", picked, signal);
    });
    if (r.ok) {
      const next = { id: r.data.id, url: URL.createObjectURL(picked) };
      stage(next);
      return next;
    }
    if (r.status === 401) return false; // the shell asks to log in again; the form stays as it is
    setError(r.message);
    pick.current?.focus();
    return false;
  };

  const remove = () => {
    stage(null);
    setStatus("");
    pick.current?.focus(); // the remove button is gone
  };

  // A save submitted during an upload waits for it, then saves with the new picture, or not at all if it failed.
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const r = await saveRun(async (signal) => {
      setStatus("");
      setInvalid(null);
      const avatar = pending.current ? await pending.current : staged;
      if (avatar === false) return null;
      setError("");
      return send<Profile>(
        "PATCH",
        "/api/admin/profile",
        {
          display_name: name,
          handle: handle || null,
          ...(avatar !== undefined && { avatar_id: avatar && avatar.id }),
        },
        signal,
      );
    });
    if (!r) return;
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
    setNameDirty(false);
    setHandleDirty(false);
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
        <input ref={file} type="file" accept="image/jpeg,image/png,image/webp" hidden onChange={upload} />
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
            onChange={(e) => (setName(e.target.value), setNameDirty(true), edited())}
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
              onChange={(e) => (setHandle(e.target.value), setHandleDirty(true), edited())}
            />
          </div>
        </div>
        <div className="save-row">
          <div className="admin-row">
            <Button type="submit" busy={saving} label="save profile" busyLabel="saving" />
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
