import { useState, type FormEvent } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";
import { ApiError } from "../lib/api";
import { useAuth } from "../lib/auth";
import { IconArrow, IconSpinner } from "../components/icons";
import "./signin.css";

type Mode = "login" | "signup";

/**
 * The front door: email and password, nothing else.
 *
 * Both modes share one form because the fields overlap and the difference is a
 * single extra input; splitting them into two pages would double the code for
 * no gain. The server owns the rules (password length, duplicate accounts,
 * throttling), so this only does the lightest client checks and shows the
 * server's message when it refuses.
 */
export default function SignIn() {
  const { session, loading, login, signup } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();

  const [mode, setMode] = useState<Mode>("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [workspaceName, setWorkspaceName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Where to go after signing in: back to whatever the guard turned away.
  const from = (location.state as { from?: string } | null)?.from ?? "/workbench";

  if (loading) {
    return (
      <div className="signin-wrap shell">
        <div className="signin-card signin-loading" aria-live="polite">
          <IconSpinner />
          <span>Checking your session…</span>
        </div>
      </div>
    );
  }
  if (session) return <Navigate to={from} replace />;

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);

    // Match the server's own minimum so the common mistake is caught before a
    // round trip. Anything subtler is the server's call.
    if (mode === "signup" && password.length < 8) {
      setError("Use at least 8 characters for your password.");
      return;
    }

    setBusy(true);
    try {
      if (mode === "login") await login({ email, password });
      else
        await signup({
          email,
          password,
          workspace_name: workspaceName.trim(),
        });
      navigate(from, { replace: true });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Something went wrong.");
    } finally {
      setBusy(false);
    }
  }

  function switchMode(next: Mode) {
    setMode(next);
    setError(null);
  }

  return (
    <div className="signin-wrap shell">
      <div className="signin-grid">
        <aside className="signin-pitch">
          <p className="signin-eyebrow mono">quickreel.io</p>
          <h1 className="signin-headline">
            Long video in.
            <br />
            Vertical clips out.
          </h1>
          <p className="signin-lede">
            Upload a podcast, an interview or a stream. QuickReel transcribes it,
            finds the moments worth stopping for, and renders them in 9:16 with
            captions.
          </p>
          <ul className="signin-points">
            <li>
              <strong>Your work stays yours.</strong> Every project belongs to a
              workspace, and only the people in it can see it.
            </li>
            <li>
              <strong>Viral potential is a prediction</strong>, not a promise.
              Scores rank the candidates; you decide what ships.
            </li>
          </ul>
        </aside>

        <section className="signin-card" aria-labelledby="signin-title">
          <div className="signin-tabs" role="tablist" aria-label="Account">
            <button
              type="button"
              role="tab"
              aria-selected={mode === "login"}
              className={mode === "login" ? "is-active" : ""}
              onClick={() => switchMode("login")}
            >
              Log in
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={mode === "signup"}
              className={mode === "signup" ? "is-active" : ""}
              onClick={() => switchMode("signup")}
            >
              Create account
            </button>
          </div>

          <h2 id="signin-title" className="signin-title">
            {mode === "login" ? "Welcome back" : "Start clipping"}
          </h2>
          <p className="signin-sub">
            {mode === "login"
              ? "Sign in with the email and password you registered."
              : "An email address and a password is all it takes."}
          </p>

          <form className="signin-form" onSubmit={onSubmit} noValidate>
            <label className="field">
              <span className="field-label">Email</span>
              <input
                type="email"
                name="email"
                autoComplete="email"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="you@studio.com"
              />
            </label>

            <label className="field">
              <span className="field-label">Password</span>
              <input
                type="password"
                name="password"
                autoComplete={mode === "login" ? "current-password" : "new-password"}
                required
                minLength={mode === "signup" ? 8 : undefined}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder={mode === "signup" ? "At least 8 characters" : "••••••••"}
              />
            </label>

            {mode === "signup" && (
              <label className="field">
                <span className="field-label">
                  Workspace name <em className="field-hint">optional</em>
                </span>
                <input
                  type="text"
                  name="workspace"
                  value={workspaceName}
                  onChange={(e) => setWorkspaceName(e.target.value)}
                  placeholder="My studio"
                />
              </label>
            )}

            {error && (
              <p className="signin-error" role="alert">
                {error}
              </p>
            )}

            <button type="submit" className="btn btn-primary signin-submit" disabled={busy}>
              {busy ? (
                <>
                  <IconSpinner /> Working…
                </>
              ) : (
                <>
                  {mode === "login" ? "Log in" : "Create account"}
                  <IconArrow />
                </>
              )}
            </button>
          </form>

          <p className="signin-foot">
            {mode === "login" ? (
              <>
                No account yet?{" "}
                <button type="button" className="linklike" onClick={() => switchMode("signup")}>
                  Create one
                </button>
              </>
            ) : (
              <>
                Already registered?{" "}
                <button type="button" className="linklike" onClick={() => switchMode("login")}>
                  Log in
                </button>
              </>
            )}
          </p>
        </section>
      </div>
    </div>
  );
}
