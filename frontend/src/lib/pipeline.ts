import type { Clip, ProjectStatus } from "../lib/types";

/** The stages the backend moves through, in order, with what each one is doing. */
export const STAGES: { key: ProjectStatus; label: string; detail: string }[] = [
  {
    key: "ingesting",
    label: "Reading the source",
    detail: "Pulling the audio and checking the timeline",
  },
  {
    key: "transcribing",
    label: "Transcribing",
    detail: "Word-level timestamps for captions",
  },
  {
    key: "analyzing",
    label: "Finding moments",
    detail: "Scoring hooks, stories and payoffs",
  },
  { key: "ready", label: "Clips ready", detail: "Ranked and trimmed" },
];

const ORDER: ProjectStatus[] = [
  "created",
  "ingesting",
  "transcribing",
  "analyzing",
  "ready",
];

/** Index of the furthest stage a status has reached, for progress display. */
export function stageIndex(status: ProjectStatus): number {
  const failed = status.endsWith("_failed");
  const base = failed ? status.replace("_failed", "") : status;
  const i = ORDER.indexOf(base as ProjectStatus);
  return i < 0 ? 0 : i;
}

export function isFailed(status: ProjectStatus): boolean {
  return status.endsWith("_failed");
}

export function isReady(status: ProjectStatus): boolean {
  return status === "ready";
}

/** SRT-ish excerpt used as a subtitle preview on a clip card. */
export function captionLines(clip: Clip, max = 2): string[] {
  return clip.transcript_excerpt
    .split(/(?<=[.!?])\s+/)
    .map((s) => s.trim())
    .filter(Boolean)
    .slice(0, max);
}
