import type {
  ChannelSubscription,
  Clip,
  HubInfo,
  OAuthProviderInfo,
  OAuthStart,
  PlatformInfo,
  Project,
  ProjectDetail,
  PublishAttempt,
  PublishTarget,
  RemoteProbe,
  ScheduledPost,
  YouTubeEvent,
} from "./types";

const BASE = import.meta.env.VITE_API_BASE ?? "/api/v1";

/** Error carrying the server's own message so the UI can show the real reason. */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${BASE}${path}`, {
      headers:
        init?.body instanceof FormData
          ? undefined
          : { "Content-Type": "application/json" },
      ...init,
    });
  } catch {
    throw new ApiError(
      "Cannot reach the QuickReel server. Start it with `make api`.",
      0,
    );
  }

  if (res.status === 204) return undefined as T;

  const text = await res.text();
  const payload = text ? safeJson(text) : null;

  if (!res.ok) {
    throw new ApiError(errorMessage(payload, res.status), res.status);
  }
  return payload as T;
}

function safeJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

/** FastAPI puts the human reason in `detail`, which may be a string or a list. */
function errorMessage(payload: unknown, status: number): string {
  if (payload && typeof payload === "object" && "detail" in payload) {
    const detail = (payload as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      const first = detail[0] as { msg?: string } | undefined;
      if (first?.msg) return first.msg;
    }
  }
  if (typeof payload === "string" && payload) return payload;
  return `Request failed (${status})`;
}

export const api = {
  createProject: (title = "Untitled project") =>
    request<Project>("/projects", {
      method: "POST",
      body: JSON.stringify({ title }),
    }),

  listProjects: () => request<Project[]>("/projects"),

  getProject: (id: string) => request<ProjectDetail>(`/projects/${id}`),

  deleteProject: (id: string) =>
    request<void>(`/projects/${id}`, { method: "DELETE" }),

  uploadVideo: (id: string, file: File, onProgress?: (pct: number) => void) =>
    new Promise<ProjectDetail>((resolve, reject) => {
      const form = new FormData();
      form.append("file", file);
      const xhr = new XMLHttpRequest();
      xhr.open("POST", `${BASE}/projects/${id}/upload`);
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) onProgress?.(Math.round((e.loaded / e.total) * 100));
      };
      xhr.onload = () => {
        const payload = xhr.responseText ? safeJson(xhr.responseText) : null;
        if (xhr.status >= 200 && xhr.status < 300) {
          resolve(payload as ProjectDetail);
        } else {
          reject(new ApiError(errorMessage(payload, xhr.status), xhr.status));
        }
      };
      xhr.onerror = () =>
        reject(new ApiError("Upload failed — the connection dropped.", 0));
      xhr.send(form);
    }),

  probeUrl: (source_url: string) =>
    request<RemoteProbe>("/projects/source-url/probe", {
      method: "POST",
      body: JSON.stringify({ source_url }),
    }),

  setSourceUrl: (id: string, source_url: string, title?: string) =>
    request<ProjectDetail>(`/projects/${id}/source-url`, {
      method: "POST",
      body: JSON.stringify({ source_url, title }),
    }),

  process: (id: string) =>
    request<Project>(`/projects/${id}/process`, { method: "POST" }),

  render: (
    id: string,
    opts: { max_clips?: number; vertical?: boolean; burn_captions?: boolean } = {},
  ) =>
    request<Clip[]>(`/projects/${id}/render`, {
      method: "POST",
      body: JSON.stringify({ max_clips: 5, vertical: true, burn_captions: true, ...opts }),
    }),

  clipDownloadUrl: (clipId: string) => `${BASE}/clips/${clipId}/download`,

  /* ---- Publishing ---- */

  listPlatforms: () =>
    request<{ platforms: PlatformInfo[] }>("/platforms").then((r) => r.platforms),

  listOAuthProviders: () =>
    request<{ providers: OAuthProviderInfo[] }>("/oauth/providers").then(
      (r) => r.providers,
    ),

  startOAuth: (platform: string) =>
    request<OAuthStart>(`/oauth/${platform}/start`),

  listPublishTargets: () => request<PublishTarget[]>("/publish-targets"),

  deletePublishTarget: (id: string) =>
    request<void>(`/publish-targets/${id}`, { method: "DELETE" }),

  listScheduledPosts: (params: { status?: string; clip_id?: string } = {}) => {
    const query = new URLSearchParams();
    if (params.status) query.set("status", params.status);
    if (params.clip_id) query.set("clip_id", params.clip_id);
    const suffix = query.toString() ? `?${query}` : "";
    return request<ScheduledPost[]>(`/scheduled-posts${suffix}`);
  },

  getScheduledPost: (id: string) => request<ScheduledPost>(`/scheduled-posts/${id}`),

  listAttempts: (postId: string) =>
    request<PublishAttempt[]>(`/scheduled-posts/${postId}/attempts`),

  schedulePost: (body: {
    clip_id: string;
    target_id: string;
    scheduled_for: string;
    caption?: string;
  }) =>
    request<ScheduledPost>("/scheduled-posts", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  cancelScheduledPost: (id: string) =>
    request<ScheduledPost>(`/scheduled-posts/${id}/cancel`, { method: "POST" }),

  publishNow: (id: string) =>
    request<ScheduledPost>(`/scheduled-posts/${id}/publish-now`, { method: "POST" }),

  /* ---- Channel ingestion (WebSub triggers) ---- */

  hubInfo: () => request<HubInfo>("/triggers/youtube/hub"),

  listChannelSubscriptions: () =>
    request<ChannelSubscription[]>("/triggers/youtube/subscriptions"),

  subscribeChannel: (body: {
    channel_id: string;
    channel_title?: string;
    authorized_by?: string;
    license_note?: string;
    auto_process?: boolean;
  }) =>
    request<ChannelSubscription>("/triggers/youtube/subscriptions", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  revokeChannel: (id: string) =>
    request<void>(`/triggers/youtube/subscriptions/${id}`, { method: "DELETE" }),

  listIngestEvents: () => request<YouTubeEvent[]>("/triggers/youtube/events"),
};
