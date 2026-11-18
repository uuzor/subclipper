import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, ApiError } from "../lib/api";
import type { Clip, ProjectDetail } from "../lib/types";
import { Timeline } from "../components/Timeline";
import { ScoreBreakdown } from "../components/ScoreBreakdown";
import { SchedulePanel } from "../components/SchedulePanel";
import {
  IconArrow,
  IconCheck,
  IconDownload,
  IconSpinner,
  IconWarn,
} from "../components/icons";
import { bytes, duration, timecode, titleCase } from "../lib/format";
import { STAGES, isFailed, isReady, stageIndex } from "../lib/pipeline";
import "./project.css";

export default function Project() {
  const { projectId = "" } = useParams();
  const [project, setProject] = useState<ProjectDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [missing, setMissing] = useState(false);
  const [rendering, setRendering] = useState(false);
  const [renderError, setRenderError] = useState<string | null>(null);
  const [activeClipId, setActiveClipId] = useState<string | undefined>();
  const timer = useRef<number | null>(null);

  const load = useCallback(async () => {
    try {
      const data = await api.getProject(projectId);
      setProject(data);
      setMissing(false);
      return data;
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) setMissing(true);
      else setError(err instanceof Error ? err.message : "Could not load project.");
      return null;
    }
  }, [projectId]);

  useEffect(() => {
    void load();
  }, [load]);

  // Poll only while the pipeline is still moving.
  useEffect(() => {
    if (!project) return;
    const done = isReady(project.status) || isFailed(project.status);
    if (done) {
      if (timer.current) window.clearTimeout(timer.current);
      return;
    }
    timer.current = window.setTimeout(() => void load(), 2500);
    return () => {
      if (timer.current) window.clearTimeout(timer.current);
    };
  }, [project, load]);

  useEffect(() => {
    if (project && project.clips.length && !activeClipId) {
      setActiveClipId(project.clips[0]?.id);
    }
  }, [project, activeClipId]);

  async function onRender() {
    setRendering(true);
    setRenderError(null);
    try {
      const clips = await api.render(projectId, { max_clips: 5 });
      setProject((p) => (p ? { ...p, clips } : p));
      setActiveClipId((id) => id ?? clips[0]?.id);
    } catch (err) {
      setRenderError(err instanceof Error ? err.message : "Rendering failed.");
    } finally {
      setRendering(false);
    }
  }

  if (missing) {
    return (
      <div className="shell pj-empty">
        <h1 className="pj-h1">That project is gone.</h1>
        <p className="pj-sub">It may have been deleted. Start another one.</p>
        <Link to="/workbench" className="btn btn-primary">
          New project
        </Link>
      </div>
    );
  }

  if (!project) {
    return (
      <div className="shell pj-loading">
        <IconSpinner size={22} />
        <span className="mono">loading project…</span>
      </div>
    );
  }

  const failed = isFailed(project.status);
  const ready = isReady(project.status);
  const stage = stageIndex(project.status);
  const hasRendered = project.clips.some((c) => c.status === "rendered");

  return (
    <div className="shell pj">
      <header className="pj-head">
        <div>
          <h1 className="pj-title">{project.title}</h1>
          <p className="mono pj-meta">
            {project.source_kind === "url" && project.source_url
              ? project.source_url
              : `upload · ${project.media ? bytes(project.media.size_bytes) : "—"}`}
          </p>
        </div>
        <div className="pj-head-right">
          {project.media && (
            <span className="mono pj-chip">
              {project.media.width}×{project.media.height}
            </span>
          )}
          {project.media && (
            <span className="mono pj-chip">{duration(project.media.duration_seconds)}</span>
          )}
          <span className={`mono pj-chip pj-chip-${ready ? "ok" : failed ? "bad" : "live"}`}>
            {project.status}
          </span>
        </div>
      </header>

      {error && (
        <div className="alert" role="alert">
          <IconWarn size={18} />
          <span>{error}</span>
        </div>
      )}

      {/* ---- still working ---- */}
      {!ready && !failed && (
        <section className="stages" aria-live="polite">
          {STAGES.map((s, i) => {
            const state = i < stage ? "done" : i === stage ? "active" : "todo";
            return (
              <div className={`stage is-${state}`} key={s.key}>
                <span className="stage-mark">
                  {state === "done" ? (
                    <IconCheck size={16} />
                  ) : state === "active" ? (
                    <IconSpinner size={16} />
                  ) : (
                    <span className="stage-dot" />
                  )}
                </span>
                <span className="stage-body">
                  <strong>{s.label}</strong>
                  <em>{s.detail}</em>
                </span>
              </div>
            );
          })}
        </section>
      )}

      {/* ---- failed ---- */}
      {failed && (
        <section className="failed">
          <IconWarn size={22} />
          <div>
            <h2 className="failed-h">This run stopped.</h2>
            <p className="failed-p">{project.error ?? "The pipeline could not finish."}</p>
            <p className="failed-hint">
              Nothing was invented to cover the gap. Fix the source and start again.
            </p>
          </div>
          <Link to="/workbench" className="btn btn-ghost">
            Start over
          </Link>
        </section>
      )}

      {/* ---- clips ---- */}
      {ready && project.media && (
        <>
          <section className="pj-overview">
            <Timeline
              durationSeconds={project.media.duration_seconds}
              clips={project.clips}
              activeClipId={activeClipId}
              onSelect={setActiveClipId}
            />
            <div className="pj-providers mono">
              <span className="pj-providers-cap">providers</span>
              {Object.entries(project.providers).map(([k, v]) => (
                <span key={k} className="pj-provider">
                  {k}: <strong>{v}</strong>
                </span>
              ))}
            </div>
          </section>

          <section className="clips-head">
            <h2 className="pj-h2">
              {project.clips.length} moment{project.clips.length === 1 ? "" : "s"} found
            </h2>
            <div className="clips-actions">
              {renderError && <span className="clips-err">{renderError}</span>}
              <button
                className="btn btn-primary"
                onClick={onRender}
                disabled={rendering || !project.clips.length}
              >
                {rendering ? (
                  <>
                    <IconSpinner size={17} /> Rendering…
                  </>
                ) : (
                  <>
                    {hasRendered ? "Re-render clips" : "Render 9:16 clips"}
                    <IconArrow size={17} />
                  </>
                )}
              </button>
            </div>
          </section>

          <div className="clips">
            {project.clips.map((clip) => (
              <ClipCard
                key={clip.id}
                clip={clip}
                active={clip.id === activeClipId}
                onFocus={() => setActiveClipId(clip.id)}
              />
            ))}
          </div>
        </>
      )}
    </div>
  );
}

function ClipCard({
  clip,
  active,
  onFocus,
}: {
  clip: Clip;
  active: boolean;
  onFocus: () => void;
}) {
  const rendered = clip.status === "rendered";
  const hookIsExcerpt = clip.hook.trim() === clip.transcript_excerpt.trim();
  return (
    <article
      className={`clip${active ? " is-active" : ""}`}
      onMouseEnter={onFocus}
      onFocus={onFocus}
    >
      <div className="clip-top">
        <span className="mono clip-rank">#{String(clip.rank).padStart(2, "0")}</span>
        <span className="mono clip-type">{titleCase(clip.moment_type)}</span>
        <span className="mono clip-range">
          {timecode(clip.start_seconds)} – {timecode(clip.end_seconds)}
        </span>
      </div>

      <h3 className="clip-title">{clip.title}</h3>

      {/* Some providers return the hook as the excerpt verbatim; never print it twice. */}
      {hookIsExcerpt ? (
        <p className="clip-excerpt clip-excerpt-sole">“{clip.transcript_excerpt}”</p>
      ) : (
        <>
          <p className="clip-hook">{clip.hook}</p>
          <p className="clip-excerpt">“{clip.transcript_excerpt}”</p>
        </>
      )}

      <p className="clip-rationale">{clip.rationale}</p>

      <ScoreBreakdown clip={clip} />

      <div className="clip-foot">
        <span className="mono clip-dur">{duration(clip.duration_seconds)}</span>
        {rendered ? (
          <a
            className="btn btn-primary clip-dl"
            href={api.clipDownloadUrl(clip.id)}
            download
          >
            <IconDownload size={17} />
            Download MP4
          </a>
        ) : (
          <span className="mono clip-pending">
            {clip.status === "rendering" ? "rendering…" : "not rendered"}
          </span>
        )}
      </div>

      {rendered && typeof clip.render_meta.size_bytes === "number" && (
        <p className="mono clip-render-note">
          1080×1920 · captions burned · {bytes(clip.render_meta.size_bytes as number)}
        </p>
      )}

      {rendered && <SchedulePanel clip={clip} />}
    </article>
  );
}
