/** Types mirroring quickreel.schemas — the HTTP contract with the FastAPI backend. */

export type ProjectStatus =
  | "created"
  | "ingesting"
  | "ingesting_failed"
  | "transcribing"
  | "transcribe_failed"
  | "analyzing"
  | "analyze_failed"
  | "ready";

export type ClipStatus = "candidate" | "rendering" | "rendered" | "render_failed";

export interface MediaAsset {
  id: string;
  path: string;
  size_bytes: number;
  duration_seconds: number;
  width: number;
  height: number;
  fps: number;
  has_audio: boolean;
}

export interface TranscriptSegment {
  start: number;
  end: number;
  text: string;
  speaker: string | null;
}

export interface Transcript {
  language: string;
  provider: string;
  text: string;
  segments: TranscriptSegment[];
}

export interface ScoreComponent {
  key: string;
  label: string;
  weight: number;
  value: number;
  contribution: number;
  explainer?: string;
}

export interface ScorePenalty {
  rule: string;
  points: number;
  reason: string;
}

/** The scoring engine's own explanation of a clip's score. */
export interface ScoreBreakdown {
  base_score?: number;
  penalty_total?: number;
  penalties?: ScorePenalty[];
  components?: ScoreComponent[];
  features?: Record<string, unknown>;
  weights?: Record<string, number>;
  boundary_adjustments?: string[];
}

export interface Clip {
  id: string;
  project_id: string;
  start_seconds: number;
  end_seconds: number;
  raw_start_seconds: number;
  raw_end_seconds: number;
  duration_seconds: number;
  title: string;
  hook: string;
  transcript_excerpt: string;
  moment_type: string;
  rationale: string;
  score: number;
  score_components: Record<string, number>;
  score_breakdown: ScoreBreakdown;
  rank: number;
  status: ClipStatus;
  render_path: string | null;
  render_meta: Record<string, unknown>;
}

export interface Project {
  id: string;
  title: string;
  source_kind: string;
  source_url: string | null;
  status: ProjectStatus;
  error: string | null;
  providers: Record<string, string>;
  created_at: string;
  updated_at: string;
}

export interface ProjectDetail extends Project {
  media: MediaAsset | null;
  transcript: Transcript | null;
  clips: Clip[];
}

export interface RemoteProbe {
  url: string;
  title: string;
  uploader: string;
  duration_seconds: number;
  thumbnail: string;
  extractor: string;
  webpage_url: string;
  downloader: string;
}

export interface RenderResult {
  clip_ids?: string[];
  max_clips?: number;
  vertical?: boolean;
  burn_captions?: boolean;
}

/* ---- Publishing ---- */

export type PlatformName = "tiktok" | "youtube_shorts" | "instagram_reels";

export type PublishTargetStatus = "active" | "needs_reauth" | "disabled";

export type ScheduledPostStatus =
  | "scheduled"
  | "publishing"
  | "published"
  | "failed"
  | "canceled";

export interface PlatformInfo {
  name: string;
  live_adapter_registered: boolean;
}

export interface OAuthProviderInfo {
  platform: string;
  scopes: string[];
  configured: boolean;
}

export interface OAuthStart {
  platform: string;
  authorize_url: string;
  state: string;
}

export interface PublishTarget {
  id: string;
  platform: string;
  display_name: string;
  external_account_id: string;
  status: PublishTargetStatus;
  status_detail: string;
  created_at: string;
  updated_at: string;
}

export interface ScheduledPost {
  id: string;
  clip_id: string;
  target_id: string;
  scheduled_for: string;
  caption: string;
  status: ScheduledPostStatus;
  external_post_id: string;
  error: string | null;
  attempts: number;
  next_attempt_at: string | null;
  published_at: string | null;
  created_at: string;
}

export interface PublishAttempt {
  id: string;
  attempt_number: number;
  ok: boolean;
  error: string;
  detail: Record<string, unknown>;
  created_at: string;
}

export type SubscriptionStatus =
  | "pending"
  | "active"
  | "expired"
  | "revoked";

export type YouTubeEventStatus =
  | "received"
  | "ignored"
  | "queued"
  | "processed"
  | "failed";

export interface ChannelSubscription {
  id: string;
  platform: string;
  channel_id: string;
  channel_title: string;
  authorized_by: string;
  license_note: string;
  status: SubscriptionStatus;
  status_detail: string;
  auto_process: boolean;
  lease_expires_at: string | null;
  last_verified_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface YouTubeEvent {
  id: string;
  channel_id: string;
  video_id: string;
  video_title: string;
  video_url: string;
  status: YouTubeEventStatus;
  reason: string;
  project_id: string | null;
  received_at: string;
  processed_at: string | null;
}

export interface HubInfo {
  hub_url: string;
  callback_url: string;
  client: string;
  signing_enabled: boolean;
  secret_configured: boolean;
}
