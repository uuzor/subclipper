import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, ApiError } from "../lib/api";
import type { ChannelSubscription, HubInfo, YouTubeEvent } from "../lib/types";
import { ingestStatusLabel, relativeTime, subscriptionStatusLabel } from "../lib/publishing";
import { IconCheck, IconSpinner, IconWarn } from "../components/icons";
import "./channels.css";

/**
 * Channels whose new uploads QuickReel watches for.
 *
 * This is the ingestion trigger, not the publishing connection: subscribing to a
 * channel means "when this channel publishes, start a project for the new
 * video". It is deliberately separate from the Accounts page, because the two
 * answer different questions and rest on different permission bases.
 */
export default function Channels() {
  const [subscriptions, setSubscriptions] = useState<ChannelSubscription[] | null>(null);
  const [events, setEvents] = useState<YouTubeEvent[]>([]);
  const [hub, setHub] = useState<HubInfo | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [channelId, setChannelId] = useState("");
  const [channelTitle, setChannelTitle] = useState("");
  const [authorizedBy, setAuthorizedBy] = useState("");
  const [licenseNote, setLicenseNote] = useState("");
  const [autoProcess, setAutoProcess] = useState(false);

  const load = useCallback(async () => {
    try {
      const [subs, evts, info] = await Promise.all([
        api.listChannelSubscriptions(),
        api.listIngestEvents(),
        api.hubInfo(),
      ]);
      setSubscriptions(subs);
      setEvents(evts.slice(0, 12));
      setHub(info);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load channels.");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function subscribe(event: React.FormEvent) {
    event.preventDefault();
    setBusy("subscribe");
    setError(null);
    try {
      await api.subscribeChannel({
        channel_id: channelId.trim(),
        channel_title: channelTitle.trim(),
        authorized_by: authorizedBy.trim(),
        license_note: licenseNote.trim(),
        auto_process: autoProcess,
      });
      setChannelId("");
      setChannelTitle("");
      setAuthorizedBy("");
      setLicenseNote("");
      setAutoProcess(false);
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not subscribe to the channel.");
    } finally {
      setBusy(null);
    }
  }

  async function revoke(id: string) {
    setBusy(id);
    try {
      await api.revokeChannel(id);
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not stop watching the channel.");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="shell channels">
      <header className="channels-head">
        <p className="eyebrow mono">Automatic ingestion</p>
        <h1>Watched channels</h1>
        <p className="lede">
          Add a channel and QuickReel starts a project whenever it publishes a new video,
          so you review clips without pasting links. Only add channels you are authorised
          to process: the authorisation you record is carried onto every clip.
        </p>
      </header>

      {error && (
        <div className="banner banner-warn" role="alert">
          <IconWarn />
          <span>{error}</span>
        </div>
      )}

      <section className="panel">
        <h2 className="panel-title">Watch a channel</h2>
        <form className="channel-form" onSubmit={subscribe}>
          <label>
            <span>YouTube channel ID</span>
            <input
              type="text"
              value={channelId}
              onChange={(e) => setChannelId(e.target.value)}
              placeholder="UCxxxxxxxxxxxxxxxxxxxxxx"
              required
              pattern="UC[A-Za-z0-9_\-]{22}"
              title="A YouTube channel id starts with UC followed by 22 characters"
            />
          </label>
          <label>
            <span>Channel name (optional)</span>
            <input
              type="text"
              value={channelTitle}
              onChange={(e) => setChannelTitle(e.target.value)}
              placeholder="The channel's display name"
            />
          </label>
          <label>
            <span>Authorised by</span>
            <input
              type="text"
              value={authorizedBy}
              onChange={(e) => setAuthorizedBy(e.target.value)}
              placeholder="You, or the person who owns the channel"
            />
          </label>
          <label className="channel-form-wide">
            <span>Licence note</span>
            <textarea
              value={licenseNote}
              onChange={(e) => setLicenseNote(e.target.value)}
              rows={2}
              placeholder="On what basis may these videos be clipped and republished?"
            />
          </label>
          <label className="channel-form-check">
            <input
              type="checkbox"
              checked={autoProcess}
              onChange={(e) => setAutoProcess(e.target.checked)}
            />
            <span>
              Process new videos automatically. Leave off to review each one first; turning
              it on spends processing time and storage without a human in the loop.
            </span>
          </label>
          <div className="channel-form-actions">
            <button type="submit" className="btn btn-primary" disabled={busy === "subscribe"}>
              {busy === "subscribe" ? <IconSpinner /> : null}
              Watch channel
            </button>
          </div>
        </form>
      </section>

      <section className="panel">
        <h2 className="panel-title">Watching</h2>
        {subscriptions === null ? (
          <p className="empty">Loading…</p>
        ) : subscriptions.length === 0 ? (
          <p className="empty">
            No channels watched yet. Add one above and its new uploads will appear under
            recent activity.
          </p>
        ) : (
          <ul className="subscription-list">
            {subscriptions.map((sub) => (
              <li key={sub.id} className="subscription-row">
                <div className="subscription-body">
                  <div className="subscription-name">
                    {sub.channel_title || sub.channel_id}
                    <span className={`chip chip-${sub.status}`}>
                      {subscriptionStatusLabel(sub.status)}
                    </span>
                    {sub.auto_process && <span className="chip">Auto</span>}
                  </div>
                  <p className="subscription-meta mono">
                    {sub.channel_id}
                    {sub.lease_expires_at && (
                      <> · lease renews {relativeTime(sub.lease_expires_at)}</>
                    )}
                  </p>
                  {sub.authorized_by && (
                    <p className="subscription-auth">Authorised by {sub.authorized_by}</p>
                  )}
                  {sub.status_detail && (
                    <p className="subscription-detail">{sub.status_detail}</p>
                  )}
                </div>
                <button
                  type="button"
                  className="btn btn-quiet btn-danger"
                  onClick={() => revoke(sub.id)}
                  disabled={busy === sub.id}
                >
                  Stop watching
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="panel">
        <h2 className="panel-title">Recent activity</h2>
        {events.length === 0 ? (
          <p className="empty">
            No notifications yet. When a watched channel publishes, the video appears here
            with what QuickReel did with it.
          </p>
        ) : (
          <table className="event-table">
            <thead>
              <tr>
                <th>Result</th>
                <th>Video</th>
                <th>When</th>
                <th>Detail</th>
              </tr>
            </thead>
            <tbody>
              {events.map((event) => (
                <tr key={event.id}>
                  <td>
                    <span className={`chip chip-${event.status}`}>
                      {ingestStatusLabel(event.status)}
                    </span>
                  </td>
                  <td>
                    {event.project_id ? (
                      <Link to={`/projects/${event.project_id}`}>
                        {event.video_title || event.video_id}
                      </Link>
                    ) : (
                      event.video_title || event.video_id
                    )}
                  </td>
                  <td className="mono">{relativeTime(event.received_at)}</td>
                  <td className="event-reason">{event.reason || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      {hub && (
        <section className="panel">
          <h2 className="panel-title">How it is wired</h2>
          <dl className="hub-info">
            <div>
              <dt>Callback URL</dt>
              <dd className="mono">{hub.callback_url}</dd>
            </div>
            <div>
              <dt>Hub</dt>
              <dd className="mono">{hub.hub_url}</dd>
            </div>
            <div>
              <dt>Mode</dt>
              <dd className="mono">
                {hub.client === "mock" ? "Simulated (no hub calls)" : "Live"}
              </dd>
            </div>
            <div>
              <dt>Callback secret</dt>
              <dd>
                {hub.secret_configured ? (
                  <span className="chip chip-ok">
                    <IconCheck /> Set
                  </span>
                ) : (
                  <span className="chip chip-warn">Not set</span>
                )}
              </dd>
            </div>
          </dl>
        </section>
      )}
    </div>
  );
}
