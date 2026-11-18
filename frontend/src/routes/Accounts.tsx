import { useCallback, useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, ApiError } from "../lib/api";
import type {
  OAuthProviderInfo,
  PlatformInfo,
  PublishTarget,
  ScheduledPost,
} from "../lib/types";
import { IconArrow, IconCheck, IconSpinner, IconWarn } from "../components/icons";
import { platformLabel, postStatusLabel } from "../lib/publishing";
import "./accounts.css";

/** Platform marks, drawn inline so the page has no image dependencies. */
function PlatformMark({ platform }: { platform: string }) {
  const common = { width: 22, height: 22, fill: "none" as const };
  if (platform === "tiktok") {
    return (
      <svg viewBox="0 0 24 24" {...common} aria-hidden="true">
        <path
          d="M14 3v10.5a3.5 3.5 0 1 1-3-3.46"
          stroke="currentColor"
          strokeWidth="1.9"
          strokeLinecap="round"
        />
        <path d="M14 5.5c.8 1.8 2.3 2.9 4.3 3.1" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" />
      </svg>
    );
  }
  if (platform === "youtube_shorts") {
    return (
      <svg viewBox="0 0 24 24" {...common} aria-hidden="true">
        <rect x="2.5" y="5.5" width="19" height="13" rx="3.4" stroke="currentColor" strokeWidth="1.8" />
        <path d="M10.4 9.6l4.4 2.4-4.4 2.4z" fill="currentColor" />
      </svg>
    );
  }
  return (
    <svg viewBox="0 0 24 24" {...common} aria-hidden="true">
      <rect x="3.5" y="3.5" width="17" height="17" rx="5" stroke="currentColor" strokeWidth="1.8" />
      <circle cx="12" cy="12" r="4.2" stroke="currentColor" strokeWidth="1.8" />
      <circle cx="17.2" cy="6.8" r="1.2" fill="currentColor" />
    </svg>
  );
}

export default function Accounts() {
  const [params, setParams] = useSearchParams();
  const [targets, setTargets] = useState<PublishTarget[] | null>(null);
  const [providers, setProviders] = useState<OAuthProviderInfo[]>([]);
  const [platforms, setPlatforms] = useState<PlatformInfo[]>([]);
  const [posts, setPosts] = useState<ScheduledPost[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  // The OAuth callback redirects back here with the outcome in the query
  // string, so the result of a connect attempt is shown in place.
  const connected = params.get("connected");
  const reason = params.get("reason");

  const load = useCallback(async () => {
    try {
      const [t, p, pl, s] = await Promise.all([
        api.listPublishTargets(),
        api.listOAuthProviders(),
        api.listPlatforms(),
        api.listScheduledPosts({ status: "published" }),
      ]);
      setTargets(t);
      setProviders(p);
      setPlatforms(pl);
      setPosts(s.slice(0, 5));
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load accounts.");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (connected === "error" && reason) {
      setNotice(`Connect failed: ${reason}`);
    } else if (connected && connected !== "error") {
      setNotice(`Connected ${platformLabel(connected)}.`);
    }
  }, [connected, reason]);

  function dismissNotice() {
    setNotice(null);
    params.delete("connected");
    params.delete("reason");
    params.delete("account");
    setParams(params, { replace: true });
  }

  async function connect(platform: string) {
    setBusy(platform);
    setError(null);
    try {
      const start = await api.startOAuth(platform);
      // Full-page navigation, not a fetch: the consent screen must be able to
      // set cookies and redirect, which XHR cannot follow across origins.
      window.location.assign(start.authorize_url);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not start the connect flow.");
      setBusy(null);
    }
  }

  async function disconnect(id: string) {
    setBusy(id);
    try {
      await api.deletePublishTarget(id);
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not disconnect.");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="accounts">
      <header className="accounts-head">
        <p className="eyebrow mono">Connected accounts</p>
        <h1>Publish destinations</h1>
        <p className="lede">
          Connect the accounts you own. QuickReel publishes your own clips to them on a
          schedule, and records what happened on every attempt.
        </p>
      </header>

      {notice && (
        <div className={`banner ${connected === "error" ? "banner-warn" : "banner-ok"}`} role="status">
          {connected === "error" ? <IconWarn /> : <IconCheck />}
          <span>{notice}</span>
          <button type="button" className="banner-close" onClick={dismissNotice} aria-label="Dismiss">
            &times;
          </button>
        </div>
      )}
      {error && (
        <div className="banner banner-warn" role="alert">
          <IconWarn />
          <span>{error}</span>
        </div>
      )}

      <section className="panel">
        <h2 className="panel-title">Available platforms</h2>
        <ul className="platform-list">
          {providers.map((provider) => {
            const live = platforms.find((p) => p.name === provider.platform);
            const linked = (targets ?? []).filter((t) => t.platform === provider.platform);
            return (
              <li key={provider.platform} className="platform-row">
                <span className="platform-mark">
                  <PlatformMark platform={provider.platform} />
                </span>
                <div className="platform-body">
                  <div className="platform-name">
                    {platformLabel(provider.platform)}
                    {linked.length > 0 && <span className="chip chip-ok">Connected</span>}
                    {!provider.configured && <span className="chip">Not configured</span>}
                    {provider.configured && live && !live.live_adapter_registered && (
                      <span className="chip chip-warn">Sandbox</span>
                    )}
                  </div>
                  <p className="platform-scopes mono">{provider.scopes.join(" · ")}</p>
                  {linked.map((target) => (
                    <div key={target.id} className="linked-account">
                      <span>{target.display_name || target.external_account_id}</span>
                      {target.status === "needs_reauth" ? (
                        <>
                          <span className="chip chip-warn">Reconnect needed</span>
                          <button
                            type="button"
                            className="btn btn-quiet"
                            onClick={() => connect(provider.platform)}
                            disabled={busy === provider.platform}
                          >
                            Reconnect
                          </button>
                        </>
                      ) : (
                        <span className="chip chip-ok">Active</span>
                      )}
                      <button
                        type="button"
                        className="btn btn-quiet btn-danger"
                        onClick={() => disconnect(target.id)}
                        disabled={busy === target.id}
                      >
                        Disconnect
                      </button>
                    </div>
                  ))}
                </div>
                <button
                  type="button"
                  className="btn btn-primary"
                  onClick={() => connect(provider.platform)}
                  disabled={!provider.configured || busy === provider.platform}
                >
                  {busy === provider.platform ? <IconSpinner /> : <IconArrow />}
                  Connect
                </button>
              </li>
            );
          })}
        </ul>
      </section>

      <section className="panel">
        <h2 className="panel-title">Recently published</h2>
        {posts.length === 0 ? (
          <p className="empty">
            Nothing published yet. Schedule a rendered clip from a project and it will appear
            here with its platform status.
          </p>
        ) : (
          <table className="post-table">
            <thead>
              <tr>
                <th>Status</th>
                <th>Caption</th>
                <th>Platform post</th>
                <th>Attempts</th>
              </tr>
            </thead>
            <tbody>
              {posts.map((post) => (
                <tr key={post.id}>
                  <td>
                    <span className={`chip chip-${post.status}`}>{postStatusLabel(post.status)}</span>
                  </td>
                  <td className="post-caption">{post.caption || "—"}</td>
                  <td className="mono post-external">{post.external_post_id || "—"}</td>
                  <td className="mono">{post.attempts}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}
