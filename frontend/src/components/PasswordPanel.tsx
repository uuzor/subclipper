import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { ApiError, api } from "../lib/api";
import { useAuth } from "../lib/auth";

/**
 * Change the account password.
 *
 * Changing a password ends every session, including this one, so the flow signs
 * the user out and sends them back to the front door to log in again with the
 * new password. That is deliberate: a password change is what you do when you
 * suspect a compromise, and leaving the old sessions alive would defeat it.
 */
export default function PasswordPanel() {
  const { logout } = useAuth();
  const navigate = useNavigate();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    if (next.length < 8) {
      setError("Use at least 8 characters for the new password.");
      return;
    }
    setBusy(true);
    try {
      await api.changePassword({ current_password: current, new_password: next });
      setDone(true);
      // The server has already invalidated the session; drop the client copy and
      // send the user to sign in again.
      await logout();
      setTimeout(() => navigate("/signin", { replace: true }), 1200);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not change the password.");
      setBusy(false);
    }
  }

  return (
    <section className="panel">
      <h2 className="panel-title">Password</h2>
      <p className="panel-note">
        Changing your password signs out every device, including this one.
      </p>

      {done ? (
        <p className="panel-notice" role="status">
          Password changed. Redirecting you to sign in…
        </p>
      ) : (
        <form className="password-form" onSubmit={onSubmit} noValidate>
          <label className="field">
            <span className="field-label">Current password</span>
            <input
              type="password"
              autoComplete="current-password"
              required
              value={current}
              onChange={(e) => setCurrent(e.target.value)}
            />
          </label>
          <label className="field">
            <span className="field-label">New password</span>
            <input
              type="password"
              autoComplete="new-password"
              required
              minLength={8}
              value={next}
              onChange={(e) => setNext(e.target.value)}
              placeholder="At least 8 characters"
            />
          </label>
          {error && (
            <p className="panel-error" role="alert">
              {error}
            </p>
          )}
          <button type="submit" className="btn" disabled={busy}>
            {busy ? "Changing…" : "Change password"}
          </button>
        </form>
      )}
    </section>
  );
}
