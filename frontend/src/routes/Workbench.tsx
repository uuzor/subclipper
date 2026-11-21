import { useCallback, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, ApiError } from "../lib/api";
import type { RemoteProbe } from "../lib/types";
import { IconArrow, IconLink, IconUpload, IconWarn } from "../components/icons";
import { duration } from "../lib/format";
import "./workbench.css";

type Tab = "file" | "url";

export default function Workbench() {
  const navigate = useNavigate();
  const [tab, setTab] = useState<Tab>("file");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // file tab
  const [file, setFile] = useState<File | null>(null);
  const [dragging, setDragging] = useState(false);
  const [progress, setProgress] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  // url tab
  const [url, setUrl] = useState("");
  const [probe, setProbe] = useState<RemoteProbe | null>(null);

  const startUpload = useCallback(
    async (chosen: File) => {
      setError(null);
      setBusy(true);
      setProgress(0);
      try {
        const project = await api.createProject(chosen.name.replace(/\.[^.]+$/, ""));
        await api.uploadVideo(project.id, chosen, setProgress);
        await api.process(project.id);
        navigate(`/projects/${project.id}`);
      } catch (err) {
        setError(err instanceof ApiError ? err.message : "Upload failed.");
        setBusy(false);
      }
    },
    [navigate],
  );

  const onDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      setDragging(false);
      const dropped = e.dataTransfer.files?.[0];
      if (dropped) {
        setFile(dropped);
        void startUpload(dropped);
      }
    },
    [startUpload],
  );

  async function onProbe() {
    setError(null);
    setProbe(null);
    setBusy(true);
    try {
      setProbe(await api.probeUrl(url.trim()));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not read that URL.");
    } finally {
      setBusy(false);
    }
  }

  async function onIngestUrl() {
    setError(null);
    setBusy(true);
    try {
      const project = await api.createProject(probe?.title || "Linked video");
      await api.setSourceUrl(project.id, url.trim(), probe?.title || undefined);
      await api.process(project.id);
      navigate(`/projects/${project.id}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not start the job.");
      setBusy(false);
    }
  }

  return (
    <div className="shell wb">
      <header className="wb-head">
        <h1 className="wb-title">New project</h1>
        <p className="wb-lede">
          Bring a long recording. Upload a file you own, or paste a link to media you
          are authorised to reuse.
        </p>
      </header>

      <div className="tabs" role="tablist" aria-label="Source type">
        <button
          role="tab"
          aria-selected={tab === "file"}
          className={`tab${tab === "file" ? " is-on" : ""}`}
          onClick={() => !busy && setTab("file")}
        >
          <IconUpload size={18} />
          Upload a file
        </button>
        <button
          role="tab"
          aria-selected={tab === "url"}
          className={`tab${tab === "url" ? " is-on" : ""}`}
          onClick={() => !busy && setTab("url")}
        >
          <IconLink size={18} />
          Paste a URL
        </button>
      </div>

      {error && (
        <div className="alert" role="alert">
          <IconWarn size={18} />
          <span>{error}</span>
        </div>
      )}

      {tab === "file" ? (
        <section
          className={`dropzone${dragging ? " is-over" : ""}${busy ? " is-busy" : ""}`}
          onDragOver={(e) => {
            e.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={onDrop}
        >
          <input
            ref={inputRef}
            type="file"
            accept="video/*,audio/*"
            hidden
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) {
                setFile(f);
                void startUpload(f);
              }
            }}
          />
          <IconUpload size={28} />
          <p className="dz-title">
            {file ? file.name : "Drop a video here"}
          </p>
          <p className="dz-sub">
            {busy
              ? `Uploading — ${progress}%`
              : "MP4, MOV, MKV, WebM or an audio file. Podcasts and streams welcome."}
          </p>

          {busy && (
            <div className="progress" aria-hidden="true">
              <span style={{ width: `${progress}%` }} />
            </div>
          )}

          {!busy && (
            <button className="btn btn-ghost" onClick={() => inputRef.current?.click()}>
              Choose a file
            </button>
          )}
        </section>
      ) : (
        <section className="url-panel">
          <label className="label" htmlFor="src-url">
            Source URL
          </label>
          <div className="url-row">
            <input
              id="src-url"
              className="field"
              type="url"
              inputMode="url"
              placeholder="https://www.youtube.com/watch?v=…"
              value={url}
              disabled={busy}
              onChange={(e) => {
                setUrl(e.target.value);
                setProbe(null);
              }}
              onKeyDown={(e) => e.key === "Enter" && url && onProbe()}
            />
            <button
              className="btn btn-ghost"
              disabled={!url || busy}
              onClick={onProbe}
            >
              Check link
            </button>
          </div>

          {probe && (
            <article className="probe">
              <div className="probe-body">
                <h2 className="probe-title">{probe.title || "Untitled media"}</h2>
                <p className="probe-meta mono">
                  {[probe.uploader, probe.extractor, duration(probe.duration_seconds)]
                    .filter(Boolean)
                    .join(" · ")}
                </p>
              </div>
              <button className="btn btn-primary" disabled={busy} onClick={onIngestUrl}>
                {busy ? "Starting…" : "Start analysis"}
                <IconArrow size={18} />
              </button>
            </article>
          )}

          <p className="url-note">
            Some hosts — YouTube among them — refuse media downloads from cloud
            servers. If the download is blocked, the job stops and tells you why. It
            will not fabricate clips.
          </p>
        </section>
      )}
    </div>
  );
}
