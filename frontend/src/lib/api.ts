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
  Session,
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
      // The session is an httpOnly cookie, so it is only sent if the browser is
      // told to include credentials. Without this every authenticated request
      // would look anonymous.
      credentials: "include",
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

  // A 401 anywhere means the session ended (expired, revoked, or logged out in
  // another tab). Broadcast it so the shell can react instead of every caller
  // having to special-case it.
  if (res.status === 401) {
    window.dispatchEvent(new Event("quickreel:unauthorized"));
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
  // A proxy in front of the API answers with its own HTML error page, which is
  // never useful to show. Explain the status instead.
  if (typeof payload === "string" && payload && !payload.trimStart().startsWith("<")) {
    return payload;
  }
  if (status === 413) {
    return "That file is too large to upload. Try a shorter recording or a lower-bitrate export.";
  }
  if (status === 0) return "Upload failed — the connection dropped.";
  return `Request failed (${status})`;
}

/**
 * Above this size a single request risks being rejected by the hosting proxy
 * before it ever reaches the API. Cloudflare's limit is 100MiB and its error is
 * an HTML page, so the app cannot explain what went wrong.
 */
const CHUNK_THRESHOLD = 64 * 1024 * 1024;

/** How many times a chunk is retried before the upload is declared failed. */
const CHUNK_RETRIES = 3;

function uploadSingle(
  id: string,
  file: File,
  onProgress?: (pct: number) => void,
): Promise<ProjectDetail> {
  return new Promise<ProjectDetail>((resolve, reject) => {
    const form = new FormData();
    form.append("file", file);
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${BASE}/projects/${id}/upload`);
    xhr.withCredentials = true;
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
  });
}

/** Send one chunk, resolving with how many bytes the server now holds. */
function sendChunk(
  projectId: string,
  uploadId: string,
  offset: number,
  blob: Blob,
): Promise<number> {
  return new Promise<number>((resolve, reject) => {
    const form = new FormData();
    form.append("file", blob, "chunk");
    const xhr = new XMLHttpRequest();
    xhr.open(
      "POST",
      `${BASE}/projects/${projectId}/upload/${uploadId}/chunk?offset=${offset}`,
    );
    xhr.withCredentials = true;
    xhr.onload = () => {
      const payload = xhr.responseText ? safeJson(xhr.responseText) : null;
      if (xhr.status >= 200 && xhr.status < 300) {
        const received = (payload as { received_bytes?: number } | null)?.received_bytes;
        resolve(typeof received === "number" ? received : offset + blob.size);
      } else {
        reject(new ApiError(errorMessage(payload, xhr.status), xhr.status));
      }
    };
    xhr.onerror = () =>
      reject(new ApiError("Upload failed — the connection dropped.", 0));
    xhr.send(form);
  });
}

async function uploadChunked(
  id: string,
  file: File,
  onProgress?: (pct: number) => void,
): Promise<ProjectDetail> {
  const init = await request<{ upload_id: string; chunk_size: number }>(
    `/projects/${id}/upload/init`,
    {
      method: "POST",
      body: JSON.stringify({ filename: file.name, total_bytes: file.size }),
    },
  );

  let offset = 0;
  while (offset < file.size) {
    const blob = file.slice(offset, offset + init.chunk_size);
    let attempt = 0;
    for (;;) {
      try {
        // The server reports the authoritative offset, so a chunk whose
        // response was lost is not appended twice.
        offset = await sendChunk(id, init.upload_id, offset, blob);
        break;
      } catch (err) {
        attempt += 1;
        if (attempt > CHUNK_RETRIES) throw err;
        await new Promise((r) => setTimeout(r, 400 * attempt));
      }
    }
    onProgress?.(Math.round((offset / file.size) * 100));
  }

  return request<ProjectDetail>(`/projects/${id}/upload/${init.upload_id}/complete`, {
    method: "POST",
    body: JSON.stringify({ filename: file.name, total_bytes: file.size }),
  });
}

export const api = {
  /* ---- Authentication and tenancy ---- */

  signup: (body: {
    email: string;
    password: string;
    display_name?: string;
    workspace_name?: string;
  }) =>
    request<Session>("/auth/signup", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  login: (body: { email: string; password: string }) =>
    request<Session>("/auth/login", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  logout: () => request<void>("/auth/logout", { method: "POST" }),

  /** The current identity, or a 401 when there is no session. */
  me: () => request<Session>("/auth/me"),

  switchWorkspace: (workspace_id: string) =>
    request<Session>("/auth/workspace", {
      method: "POST",
      body: JSON.stringify({ workspace_id }),
    }),

  changePassword: (body: { current_password: string; new_password: string }) =>
    request<void>("/auth/password", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  createProject: (title = "Untitled project") =>
    request<Project>("/projects", {
      method: "POST",
      body: JSON.stringify({ title }),
    }),

  listProjects: () => request<Project[]>("/projects"),

  getProject: (id: string) => request<ProjectDetail>(`/projects/${id}`),

  deleteProject: (id: string) =>
    request<void>(`/projects/${id}`, { method: "DELETE" }),

  /**
   * Upload a video, switching to chunked transfer for large files.
   *
   * A proxy in front of the API caps the request body - Cloudflare rejects
   * anything over 100MiB with an HTML 413 that the app never sees, so a single
   * request for a podcast episode fails with no useful error. Files above
   * `CHUNK_THRESHOLD` are therefore sent in pieces, and a piece that fails is
   * retried from the offset the server reports.
   */
  uploadVideo: (id: string, file: File, onProgress?: (pct: number) => void) =>
    file.size > CHUNK_THRESHOLD
      ? uploadChunked(id, file, onProgress)
      : uploadSingle(id, file, onProgress),

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
