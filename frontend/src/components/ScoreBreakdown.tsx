import type { Clip, ScoreComponent } from "../lib/types";
import { titleCase } from "../lib/format";

/**
 * Score shown as named contributions, not a single dial.
 * Prefers the engine's own breakdown (components with weights and points) and
 * falls back to the flat component map for older rows.
 */

const FALLBACK_LABELS: Record<string, string> = {
  emotional_intensity: "Emotional intensity",
  hook_strength: "Hook strength",
  storytelling: "Storytelling",
  shareability: "Shareability",
  audio_visual_engagement: "Audio + visual",
  standalone_context: "Standalone context",
};

function readComponents(clip: Clip): ScoreComponent[] {
  const raw = clip.score_breakdown?.components;
  if (Array.isArray(raw) && raw.length) {
    return raw;
  }
  return Object.entries(clip.score_components ?? {}).map(([key, value]) => ({
    key,
    label: FALLBACK_LABELS[key] ?? titleCase(key),
    weight: 0,
    value,
    contribution: value * 100,
  }));
}

export function ScoreBreakdown({ clip }: { clip: Clip }) {
  const components = readComponents(clip);
  const base = clip.score_breakdown?.base_score;
  const penalty = clip.score_breakdown?.penalty_total;
  const adjustments = clip.score_breakdown?.boundary_adjustments;

  return (
    <div className="breakdown">
      <div className="breakdown-head">
        <span className="mono breakdown-num">{clip.score.toFixed(1)}</span>
        <span className="mono breakdown-cap">predicted engagement</span>
      </div>

      <ul className="breakdown-list">
        {components.map((c) => (
          <li key={c.key}>
            <span className="bar-label" title={c.explainer}>
              {c.label}
              {c.weight > 0 && (
                <em className="bar-weight mono">{Math.round(c.weight * 100)}%</em>
              )}
            </span>
            <span className="bar-track" aria-hidden="true">
              <span
                className="bar-fill"
                style={{ width: `${Math.min(100, Math.max(2, c.value * 100))}%` }}
              />
            </span>
            <span className="mono bar-val">{(c.value * 100).toFixed(0)}</span>
          </li>
        ))}
      </ul>

      <p className="breakdown-note mono">
        {typeof base === "number" && <>base {base.toFixed(1)}</>}
        {typeof penalty === "number" && penalty > 0 && <> · penalty −{penalty.toFixed(1)}</>}
      </p>

      {Array.isArray(adjustments) && adjustments.length > 0 && (
        <ul className="breakdown-adjust">
          {adjustments.map((a, i) => (
            <li key={i}>{String(a)}</li>
          ))}
        </ul>
      )}
    </div>
  );
}
