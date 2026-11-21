import { useEffect, useRef, useState } from "react";
import { Link, NavLink, Outlet, useNavigate } from "react-router-dom";
import { useAuth } from "../lib/auth";
import "./shell.css";

function Wordmark() {
  return (
    <Link to="/" className="wordmark" aria-label="QuickReel home">
      <span className="wordmark-cut" aria-hidden="true">
        <svg viewBox="0 0 22 22" width="22" height="22" fill="none">
          <circle cx="5" cy="5" r="2.6" stroke="currentColor" strokeWidth="1.8" />
          <circle cx="5" cy="17" r="2.6" stroke="currentColor" strokeWidth="1.8" />
          <path d="M7 6.6 19 17M7 15.4 19 5" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
        </svg>
      </span>
      <span className="wordmark-text">
        Quick<em>Reel</em>
      </span>
    </Link>
  );
}

/**
 * The account control: who you are, which workspace you are acting in, and the
 * way out. The workspace switch is the tenancy made visible — the same login
 * can hold several workspaces, and every request is scoped to the chosen one.
 */
function AccountMenu() {
  const { session, logout, switchWorkspace } = useAuth();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    function onDocClick(event: MouseEvent) {
      if (ref.current && !ref.current.contains(event.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", onDocClick);
    return () => document.removeEventListener("mousedown", onDocClick);
  }, [open]);

  if (!session) {
    return (
      <Link to="/signin" className="btn btn-primary topbar-cta">
        Log in
      </Link>
    );
  }

  const initial = (session.user.email[0] ?? "?").toUpperCase();

  async function onLogout() {
    setBusy(true);
    try {
      await logout();
      navigate("/signin", { replace: true });
    } finally {
      setBusy(false);
    }
  }

  async function onSwitch(id: string) {
    if (id === session!.workspace.id) return setOpen(false);
    setBusy(true);
    try {
      await switchWorkspace(id);
      setOpen(false);
      // Reload so every page re-fetches under the new tenant rather than
      // showing the previous workspace's data until the next navigation.
      navigate(0);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="account" ref={ref}>
      <button
        type="button"
        className="account-trigger"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        <span className="account-avatar" aria-hidden="true">
          {initial}
        </span>
        <span className="account-who">
          <span className="account-email">{session.user.email}</span>
          <span className="account-workspace mono">{session.workspace.name}</span>
        </span>
      </button>

      {open && (
        <div className="account-menu" role="menu">
          <div className="account-menu-head">
            <span className="account-menu-label mono">Workspace</span>
            <span className="account-menu-name">{session.workspace.name}</span>
          </div>

          {session.workspaces.length > 1 && (
            <div className="account-switch">
              {session.workspaces.map((ws) => (
                <button
                  key={ws.id}
                  type="button"
                  role="menuitemradio"
                  aria-checked={ws.id === session.workspace.id}
                  className={ws.id === session.workspace.id ? "is-current" : ""}
                  disabled={busy}
                  onClick={() => onSwitch(ws.id)}
                >
                  {ws.name}
                </button>
              ))}
            </div>
          )}

          <button
            type="button"
            role="menuitem"
            className="account-signout"
            disabled={busy}
            onClick={onLogout}
          >
            {busy ? "Signing out…" : "Sign out"}
          </button>
        </div>
      )}
    </div>
  );
}

export default function Shell() {
  const { session } = useAuth();

  return (
    <div className="app">
      <header className="topbar">
        <div className="shell topbar-inner">
          <Wordmark />
          <nav className="topnav" aria-label="Primary">
            <NavLink to="/" end>
              Home
            </NavLink>
            {session && (
              <>
                <NavLink to="/workbench">Workbench</NavLink>
                <NavLink to="/channels">Channels</NavLink>
                <NavLink to="/accounts">Accounts</NavLink>
              </>
            )}
          </nav>
          <AccountMenu />
        </div>
      </header>

      <main id="main">
        <Outlet />
      </main>

      <footer className="footer">
        <div className="shell footer-inner">
          <div className="footer-brand">
            <Wordmark />
            <p className="footer-line">
              Long video in. Vertical clips out. Built on Whisper, Gemini and FFmpeg.
            </p>
          </div>
          <div className="footer-meta mono">
            <span>clipviralmoment.com</span>
            <span>Viral potential is a prediction, not a promise.</span>
            <span className="footer-legal">
              <a href="/privacy">Privacy</a>
              <a href="/terms">Terms</a>
            </span>
          </div>
        </div>
      </footer>
    </div>
  );
}
