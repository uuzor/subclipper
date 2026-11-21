/** Display helpers shared by the publishing screens. */

import type { ScheduledPostStatus } from "./types";

const PLATFORM_LABELS: Record<string, string> = {
  tiktok: "TikTok",
  youtube_shorts: "YouTube Shorts",
  instagram_reels: "Instagram Reels",
};

export function platformLabel(platform: string): string {
  return PLATFORM_LABELS[platform] ?? platform;
}

const POST_STATUS_LABELS: Record<ScheduledPostStatus, string> = {
  scheduled: "Scheduled",
  publishing: "Publishing",
  published: "Published",
  failed: "Failed",
  canceled: "Canceled",
};

export function postStatusLabel(status: ScheduledPostStatus): string {
  return POST_STATUS_LABELS[status] ?? status;
}

/** A human summary of when a post is due, relative to now. */
export function relativeTime(iso: string): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "—";
  const deltaSeconds = Math.round((then - Date.now()) / 1000);
  const past = deltaSeconds < 0;
  const seconds = Math.abs(deltaSeconds);
  const units: [number, string][] = [
    [60, "second"],
    [60, "minute"],
    [24, "hour"],
    [7, "day"],
    [4.35, "week"],
    [12, "month"],
  ];
  let value = seconds;
  let unit = "second";
  for (const [size, name] of units) {
    if (value < size) break;
    value /= size;
    unit = name;
  }
  const rounded = Math.round(value);
  const plural = rounded === 1 ? "" : "s";
  return past ? `${rounded} ${unit}${plural} ago` : `in ${rounded} ${unit}${plural}`;
}

/** Local datetime string for a datetime-local input, from an ISO instant. */
export function toLocalInputValue(date: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    `T${pad(date.getHours())}:${pad(date.getMinutes())}`
  );
}

const SUBSCRIPTION_STATUS_LABELS: Record<string, string> = {
  pending: "Pending verification",
  active: "Active",
  expired: "Expired",
  revoked: "Revoked",
};

export function subscriptionStatusLabel(status: string): string {
  return SUBSCRIPTION_STATUS_LABELS[status] ?? status;
}

const INGEST_STATUS_LABELS: Record<string, string> = {
  received: "Received",
  ignored: "Skipped",
  queued: "Queued for review",
  processed: "Processed",
  failed: "Failed",
};

export function ingestStatusLabel(status: string): string {
  return INGEST_STATUS_LABELS[status] ?? status;
}
