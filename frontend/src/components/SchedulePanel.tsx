import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, ApiError } from "../lib/api";
import type { Clip, PublishTarget, ScheduledPost } from "../lib/types";
import { IconArrow, IconCheck, IconSpinner, IconWarn } from "./icons";
import { platformLabel, postStatusLabel, relativeTime, toLocalInputValue } from "../lib/publishing";
import "./schedule.css";

/** Default publish time: one hour from now, rounded to the next five minutes. */
function defaultWhen(): string {
  const when = new Date(Date.now() + 60 * 60 * 1000);
  when.setMinutes(Math.ceil(when.getMinutes() / 5) * 5, 0, 0);
  return toLocalInputValue(when);
}

/**
 * The post-composition screen: pick an account, write a caption, choose a time.
 *
 * Rendered inline on the project page rather than as a modal so the clip being
 * scheduled stays visible while the caption is written, which is how the
 * decision is actually made.
 */
export function SchedulePanel({ clip }: { clip: Clip }) {
  const [targets, setTargets] = useState<PublishTarget[] | null>(null);
  const [targetId, setTargetId] = useState("");
  const [caption, setCaption] = useState(clip.hook || clip.title);
  const [when, setWhen] = useState(defaultWhen);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<ScheduledPost | null>(null);

  const load = useCallback(async () => {
    try {
      const list = await api.listPublishTargets();
      setTargets(list);
      const active = list.find((t) => t.status === "active");
      setTargetId((current) => current || active?.id || "");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load accounts.");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    setCaption(clip.hook || clip.title);
    setCreated(null);
  }, [clip.id, clip.hook, clip.title]);

  async function onSchedule() {
    if (!targetId) return;
    setBusy(true);
    setError(null);
    try {
      const post = await api.schedulePost({
        clip_id: clip.id,
        target_id: targetId,
        // The API expects an absolute instant; the input is local wall time.
        scheduled_for: new Date(when).toISOString(),
        caption,
      });
      setCreated(post);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not schedule the clip.");
    } finally {
      setBusy(false);
    }
  }

  const rendered = clip.status === "rendered";
  const noAccounts = targets !== null && targets.length === 0;

  if (!rendered) {
    return (
      <div className="sched sched-blocked">
        <IconWarn size={18} />
        <p>Render this clip before scheduling it. A post without a file can only fail.</p>
      </div>
    );
  }

  return (
    <div className="sched">
      <div className="sched-head">
        <h3 className="sched-title">Schedule this clip</h3>
        <Link to="/accounts" className="sched-manage mono">
          Manage accounts
        </Link>
      </div>

      {noAccounts ? (
        <p className="sched-empty">
          No accounts connected yet. <Link to="/accounts">Connect one</Link> to publish
          this clip.
        </p>
      ) : (
        <div className="sched-grid">
          <label className="sched-field">
            <span className="sched-label">Account</span>
            <select
              className="field"
              value={targetId}
              onChange={(e) => setTargetId(e.target.value)}
            >
              {(targets ?? []).map((target) => (
                <option key={target.id} value={target.id} disabled={target.status !== "active"}>
                  {platformLabel(target.platform)} · {target.display_name || "account"}
                  {target.status === "needs_reauth" ? " (reconnect needed)" : ""}
                </option>
              ))}
            </select>
          </label>

          <label className="sched-field">
            <span className="sched-label">Publish at</span>
            <input
              className="field"
              type="datetime-local"
              value={when}
              onChange={(e) => setWhen(e.target.value)}
            />
          </label>

          <label className="sched-field sched-field-wide">
            <span className="sched-label">
              Caption <span className="sched-count mono">{caption.length}/2200</span>
            </span>
            <textarea
              className="field sched-caption"
              value={caption}
              maxLength={2200}
              rows={3}
              onChange={(e) => setCaption(e.target.value)}
            />
          </label>

          <div className="sched-actions">
            <button
              type="button"
              className="btn btn-primary"
              onClick={onSchedule}
              disabled={busy || !targetId}
            >
              {busy ? <IconSpinner size={17} /> : <IconArrow size={17} />}
              Schedule post
            </button>
          </div>
        </div>
      )}

      {error && (
        <div className="banner banner-warn sched-banner" role="alert">
          <IconWarn />
          <span>{error}</span>
        </div>
      )}

      {created && (
        <div className="banner banner-ok sched-banner" role="status">
          <IconCheck />
          <span>
            {postStatusLabel(created.status)} — due {relativeTime(created.scheduled_for)}.
            Track it on the <Link to="/accounts">accounts page</Link>.
          </span>
        </div>
      )}
    </div>
  );
}
