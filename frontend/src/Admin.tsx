import { type MouseEvent, Suspense, useEffect, useRef, useState } from "react";
import { AdminHead, Avatar, Footer } from "./App";
import { checkSession, login, type ProfileChannel, profileChannel, logout as signOut, type Result } from "./adminApi";
import { adminPages } from "./adminPages";
import { Button, sendOrExpire } from "./EntryEditor";
import type { Profile } from "./timelineTypes";
import { useRequest } from "./useRequest";

// The private admin area: a password form, then the shell (nav, the current page, the footer) around the pages in
// adminPages.tsx. It is lazy loaded, so public pages never download it, and nothing on the site links here.

export default function Admin() {
  const [auth, setAuth] = useState<"loading" | "unavailable" | "out" | "in">("loading");
  // The pages are kept (hidden) while the session is out after a 401, so unsaved text survives logging in again.
  const [mounted, setMounted] = useState(false);
  const [notice, setNotice] = useState("");
  const { run: logoutRun, busy: loggingOut } = useRequest("submit");
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

  // The URL picks the page; a signed-out visit keeps it, so logging in shows the page asked for.
  const [path, setPath] = useState(location.pathname);
  // Pages stay mounted once shown, so switching pages keeps their unsaved text.
  const [visited, setVisited] = useState([location.pathname]);
  const current = adminPages.find((p) => p.path === path);
  const content = useRef<HTMLDivElement>(null);
  // Set by a nav click or back/forward: the shown page's h1 takes focus once it has loaded.
  const focusPending = useRef(false);
  const focusHeading = () => {
    const h1 = content.current?.querySelector<HTMLElement>(".admin-page:not([hidden]) h1");
    if (!focusPending.current || !h1) return;
    focusPending.current = false;
    h1.focus();
  };
  const show = (to: string) => {
    focusPending.current = true;
    setPath(to);
    setVisited((v) => (v.includes(to) ? v : [...v, to]));
  };
  useEffect(() => {
    const back = () => show(location.pathname);
    addEventListener("popstate", back);
    return () => removeEventListener("popstate", back);
  }, []);
  useEffect(focusHeading, [path]);
  // Plain links everywhere; a click on one to an admin page swaps the page in place instead of loading the document.
  const navigate = (e: MouseEvent) => {
    const a = (e.target as Element).closest("a");
    const to = a?.getAttribute("href");
    if (!to || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    if (!adminPages.some((p) => p.path === to)) return;
    e.preventDefault();
    if (to === path) return;
    history.pushState(null, "", to);
    show(to);
  };

  // Aborted on unmount: the shell's own requests (the first session check, retry, the shared profile load).
  const life = useRef<AbortController>(null!);

  // The public profile, for the nav and the pages; fetched once, again only after a failure.
  const [profile, setProfile] = useState<Profile | null>(null);
  const profileFlight = useRef<Promise<Result<Profile>> | null>(null);
  const loadProfile = () => {
    if (profileFlight.current) return profileFlight.current;
    const flight: Promise<Result<Profile>> = (profileFlight.current = send<Profile>("GET", "/api/profile", undefined, life.current.signal).then((r) => {
      if (profileFlight.current !== flight) return r; // a newer saved profile came in meanwhile
      if (r.ok) setProfile(r.data);
      else profileFlight.current = null;
      return r;
    }));
    return flight;
  };
  // A saved profile, from this tab's profile page or another tab's; it supersedes a load still running.
  const applyProfile = (p: Profile) => {
    profileFlight.current = Promise.resolve({ ok: true, data: p });
    setProfile(p);
  };
  const channel = useRef<ProfileChannel>(null);
  useEffect(() => {
    const c = (channel.current = profileChannel(applyProfile));
    return () => c.close();
  }, []);
  const saveProfile = (p: Profile) => {
    applyProfile(p);
    channel.current?.postMessage(p);
  };

  const send = sendOrExpire(() => expire());

  // The control that had focus when the session ran out; logging in again puts focus back on it.
  const focusBefore = useRef<Element | null>(null);
  const expire = () => {
    focusBefore.current ??= document.activeElement; // the first 401's: later ones see the hidden pages
    setNotice("session expired, log in again");
    setAuth("out");
  };

  // Returns whether the session is in, so login can tell a cookie that did not stick. signal is login's run, or life.
  const session = async (signal: AbortSignal) => {
    const r = await checkSession(signal);
    if (signal.aborted) return false;
    if (!r.ok) {
      setNotice(r.message);
      setAuth("unavailable");
      return false;
    }
    setAuth(r.data.authenticated ? "in" : "out");
    if (r.data.authenticated) {
      setNotice("");
      setMounted(true);
      loadProfile();
    }
    return r.data.authenticated;
  };

  useEffect(() => {
    const c = (life.current = new AbortController()); // made here, not in useRef: strict mode replays this effect
    session(c.signal);
    return () => c.abort();
  }, []);

  const logout = async () => {
    const r = await logoutRun((signal) => signOut(signal));
    if (!r.ok && r.status !== 401) return setNotice(r.message);
    // A deliberate logout drops unsaved text with the pages; the URL stays.
    setMounted(false);
    setVisited([path]);
    focusBefore.current = null;
    setNotice("");
    setAuth("out");
  };

  const signedIn = auth === "in";
  useEffect(() => {
    const before = focusBefore.current;
    if (!signedIn || !before) return;
    focusBefore.current = null;
    if (before instanceof HTMLElement && before !== document.body && before.isConnected && !before.closest("[hidden]")) before.focus();
    else (focusPending.current = true), focusHeading();
  }, [signedIn]);
  // The server titles by the admin_hint cookie (sign in, or the path); the session check corrects a stale hint.
  useEffect(() => {
    if (auth === "out") document.title = "sign in · admin";
    if (signedIn && current) document.title = `${current.label} · admin`;
  }, [path, auth]);

  return (
    <main className={signedIn ? "admin admin-shell" : undefined} onClick={navigate}>
      {!signedIn && (
        <article className="admin">
          <AdminHead />
          {auth === "loading" && slow && (
            <p className="admin-section muted" role="status">
              loading
            </p>
          )}
          {auth === "unavailable" && (
            <div className="admin-section admin-row">
              <p className="error" role="alert">
                {notice}
              </p>
              <Button onClick={() => (setAuth("loading"), session(life.current.signal))} label="retry" />
            </div>
          )}
          {auth === "out" && (
            <section className="admin-section sign-in">
              <h2>sign in</h2>
              <Login session={session} notice={notice} />
            </section>
          )}
        </article>
      )}
      {mounted && (
        <nav className="admin-nav" aria-label="admin" hidden={!signedIn}>
          <a className="admin-title" href="/admin">
            admin
          </a>
          <ul>
            {adminPages.map((p) => (
              <li key={p.path}>
                <a href={p.path} aria-current={p.path === path ? "page" : undefined}>
                  {p.label}
                </a>
              </li>
            ))}
          </ul>
          <div className="admin-session">
            {profile && (
              <span className="identity">
                <Avatar profile={profile} size={24} />
                <span className="name">{profile.display_name}</span>
              </span>
            )}
            <Button onClick={logout} busy={loggingOut} label="log out" busyLabel="logging out" />
          </div>
        </nav>
      )}
      {mounted && (
        <div className="admin-content" ref={content} hidden={!signedIn}>
          <p className="error" role="alert">
            {signedIn && notice}
          </p>
          {adminPages
            .filter((p) => visited.includes(p.path))
            .map(({ path: at, Page }) => (
              <div key={at} className="admin-page" hidden={at !== path}>
                <Suspense>
                  <Page send={send} expire={expire} profile={profile} loadProfile={loadProfile} setProfile={saveProfile} active={signedIn && at === path} />
                  <Loaded onLoad={focusHeading} />
                </Suspense>
              </div>
            ))}
          <Footer />
        </div>
      )}
      {!signedIn && <Footer />}
    </main>
  );
}

// Mounts with the lazy page beside it in one Suspense, so its effect runs once that page is on screen.
function Loaded({ onLoad }: { onLoad: () => void }) {
  useEffect(onLoad, []);
  return null;
}

function Login({ session, notice }: { session: (signal: AbortSignal) => Promise<boolean>; notice: string }) {
  const { run, busy } = useRequest("submit");
  const [error, setError] = useState(notice);
  const [blockedFor, setBlockedFor] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (!blockedFor) return;
    const t = setTimeout(() => setBlockedFor(0), blockedFor * 1000);
    return () => clearTimeout(t);
  }, [blockedFor]);
  const submit = async () => {
    const field = input.current!;
    if (blockedFor || !field.value) return;
    const { r, ok } = await run(async (signal) => {
      setError("");
      // Read from the field at submit time: the password never goes into React state.
      const r = await login(field.value, signal);
      field.value = "";
      return { r, ok: r.ok && (await session(signal)) };
    });
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
