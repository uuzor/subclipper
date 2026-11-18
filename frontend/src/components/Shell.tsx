import { Link, NavLink, Outlet } from "react-router-dom";
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

export default function Shell() {
  return (
    <div className="app">
      <header className="topbar">
        <div className="shell topbar-inner">
          <Wordmark />
          <nav className="topnav" aria-label="Primary">
            <NavLink to="/" end>
              Home
            </NavLink>
            <NavLink to="/workbench">Workbench</NavLink>
            <NavLink to="/channels">Channels</NavLink>
            <NavLink to="/accounts">Accounts</NavLink>
          </nav>
          <Link to="/workbench" className="btn btn-primary topbar-cta">
            Open workbench
          </Link>
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
