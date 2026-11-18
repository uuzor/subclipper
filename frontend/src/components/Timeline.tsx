import { useMemo } from "react";
import type { Clip } from "../lib/types";
import { timecode } from "../lib/format";

/**
 * The signature element: one long source timeline with the detected moments
 * struck through it. This is the product's whole claim in a single figure —
 * hours in, a handful of moments out.
 *
 * The waveform is generated deterministically from the clip boundaries so the
 * same project always draws the same picture (no random jitter on re-render).
 */
interface Props {
  durationSeconds: number;
  clips: Clip[];
  activeClipId?: string;
  onSelect?: (clipId: string) => void;
}

const BARS = 180;

function envelope(seed: number, i: number): number {
  // Deterministic pseudo-audio: layered sines + a hash, in [0.08, 1].
  const t = i / BARS;
  const base =
    0.42 +
    0.3 * Math.sin(t * 21 + seed) * Math.sin(t * 6.1 + seed * 0.7) +
    0.22 * Math.sin(t * 47.3 + seed * 2.1);
  const hash = Math.abs(Math.sin((i + 1) * (seed + 12.9898) * 78.233)) % 1;
  return Math.min(1, Math.max(0.08, base * 0.7 + hash * 0.35));
}

export function Timeline({ durationSeconds, clips, activeClipId, onSelect }: Props) {
  const dur = Math.max(1, durationSeconds);

  const bars = useMemo(
    () => Array.from({ length: BARS }, (_, i) => envelope(clips.length + 3, i)),
    [clips.length],
  );

  const spans = clips.map((c) => ({
    clip: c,
    left: (c.start_seconds / dur) * 100,
    width: Math.max(0.6, ((c.end_seconds - c.start_seconds) / dur) * 100),
  }));

  return (
    <div className="timeline" role="group" aria-label="Detected moments on the source timeline">
      <div className="timeline-head">
        <span className="mono timeline-label">SOURCE</span>
        <span className="mono timeline-dur">{timecode(dur)}</span>
      </div>

      <div className="timeline-track">
        <div className="wave" aria-hidden="true">
          {bars.map((v, i) => (
            <span key={i} style={{ height: `${Math.round(v * 100)}%` }} />
          ))}
        </div>

        {spans.map(({ clip, left, width }) => {
          const active = clip.id === activeClipId;
          return (
            <button
              key={clip.id}
              type="button"
              className={`mark${active ? " is-active" : ""}`}
              style={{ left: `${left}%`, width: `${width}%` }}
              onClick={() => onSelect?.(clip.id)}
              aria-label={`Clip ${clip.rank}: ${clip.title}, ${timecode(clip.start_seconds)} to ${timecode(clip.end_seconds)}`}
            >
              <span className="mark-rank mono">{String(clip.rank).padStart(2, "0")}</span>
            </button>
          );
        })}
      </div>

      <div className="timeline-foot mono" aria-hidden="true">
        <span>0:00</span>
        <span>{timecode(dur / 2)}</span>
        <span>{timecode(dur)}</span>
      </div>
    </div>
  );
}
