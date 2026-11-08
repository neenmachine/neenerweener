import { useEffect, useState } from "react";
import { Avatar } from "./App";
import type { AdminPageProps } from "./adminPages";
import { Button } from "./EntryEditor";

// A link row per page, with what that page holds: the entry count and the poster.
export default function AdminOverview({ send, profile, loadProfile }: AdminPageProps) {
  const [count, setCount] = useState<number | null>(null);
  const [state, setState] = useState<"loading" | "ok" | "failed">("loading");
  const [error, setError] = useState("");

  const load = async () => {
    setState("loading");
    const [c, p] = await Promise.all([send<{ entry_count: number }>("GET", "/api/admin/overview"), loadProfile()]);
    const failed = !c.ok ? c : !p.ok ? p : null;
    if (c.ok) setCount(c.data.entry_count);
    setError(failed ? failed.message : "");
    setState(failed ? "failed" : "ok");
  };

  useEffect(() => {
    load();
  }, []);

  return (
    <>
      <h1 tabIndex={-1}>overview</h1>
      {state === "loading" && <p className="admin-state muted">loading</p>}
      {state === "failed" && (
        <div className="admin-state admin-row">
          <p className="error" role="alert">
            {error}
          </p>
          <Button onClick={load} label="retry" />
        </div>
      )}
      {state === "ok" && profile && count !== null && (
        <ul className="admin-links">
          <li>
            <a href="/admin/timeline">
              <span className="label">timeline</span>
              <span className="muted">{count === 1 ? "1 entry" : `${count.toLocaleString("en")} entries`}</span>
            </a>
          </li>
          <li>
            <a href="/admin/profile">
              <span className="label">profile</span>
              <span className="identity">
                <Avatar profile={profile} size={24} />
                <span className="name">{profile.display_name}</span>
                {profile.handle && <span className="muted">@{profile.handle}</span>}
              </span>
            </a>
          </li>
        </ul>
      )}
    </>
  );
}
