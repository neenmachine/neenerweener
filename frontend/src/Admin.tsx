import { type MouseEvent, Suspense, useEffect, useRef, useState } from "react";
import { AdminHead, Avatar, Footer } from "./App";
import { checkSession, login, logout as signOut, type Result } from "./adminApi";
import { adminPages } from "./adminPages";
import { Button, sendOrExpire } from "./EntryEditor";
import type { Profile } from "./timelineTypes";

// The private admin area: a password form, then the shell (nav, the current page, the footer) around the pages in
// adminPages.tsx. It is lazy loaded, so public pages never download it, and nothing on the site links here.

export default function Admin() {
  const [auth, setAuth] = useState<"loading" | "unavailable" | "out" | "in">("loading");
  // The pages are kept (hidden) while the session is out after a 401, so unsaved text survives logging in again.
  const [mounted, setMounted] = useState(false);
  const [notice, setNotice] = useState("");
  const [loggingOut, setLoggingOut] = useState(false);
  const outFlight = useRef(false);
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

  // The public profile, for the nav and the pages; fetched once, again only after a failure.
  const [profile, setProfile] = useState<Profile | null>(null);
  const profileFlight = useRef<Promise<Result<Profile>> | null>(null);
  const loadProfile = () =>
    (profileFlight.current ??= send<Profile>("GET", "/api/profile").then((r) => {
      if (r.ok) setProfile(r.data);
      else profileFlight.current = null;
      return r;
    }));

  const send = sendOrExpire(() => expire());

  const expire = () => {
    setNotice("session expired, log in again");
    setAuth("out");
  };

  // Returns whether the session is in, so login can tell a cookie that did not stick.
  const session = async () => {
    const r = await checkSession();
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
    session();
  }, []);

  const logout = async () => {
    if (outFlight.current) return;
    outFlight.current = true;
    setLoggingOut(true);
    const r = await signOut();
    outFlight.current = false;
    setLoggingOut(false);
    if (!r.ok && r.status !== 401) return setNotice(r.message);
    // A deliberate logout drops unsaved text with the pages; the URL stays.
    setMounted(false);
    setVisited([path]);
    setNotice("");
    setAuth("out");
  };

  const signedIn = auth === "in";
  // The server titles an admin page by its path (it cannot know the session); signed out, the sign-in form is the page.
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
              <Button onClick={() => (setAuth("loading"), session())} label="retry" />
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
                  <Page send={send} expire={expire} profile={profile} loadProfile={loadProfile} active={signedIn && at === path} />
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

function Login({ session, notice }: { session: () => Promise<boolean>; notice: string }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(notice);
  const [blockedFor, setBlockedFor] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  const inFlight = useRef(false);
  useEffect(() => {
    if (!blockedFor) return;
    const t = setTimeout(() => setBlockedFor(0), blockedFor * 1000);
    return () => clearTimeout(t);
  }, [blockedFor]);
  const submit = async () => {
    const field = input.current!;
    if (inFlight.current || blockedFor || !field.value) return;
    inFlight.current = true;
    setBusy(true);
    setError("");
    // Read from the field at submit time: the password never goes into React state.
    const r = await login(field.value);
    field.value = "";
    const ok = r.ok && (await session());
    inFlight.current = false;
    setBusy(false);
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
