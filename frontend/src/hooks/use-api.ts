"use client";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { toast } from "@/components/toast";

async function get<T = any>(url: string): Promise<T> {
  // Overlap guard for polling: if a GET for the same URL is already in
  // flight (e.g. a 30s refetch racing a slow response past the axios 30s
  // timeout), share its promise instead of stacking another request.
  const pending = inflightGets.get(url);
  if (pending) return pending as Promise<T>;
  const req = api.get(url).then(({ data }) => data as T);
  inflightGets.set(url, req);
  try {
    return await req;
  } finally {
    inflightGets.delete(url);
  }
}

// In-flight GETs keyed by URL — see get() above.
const inflightGets = new Map<string, Promise<unknown>>();

/** Show the API's own result payload as a toast so every action gives visible feedback.
 *  Returns true when it toasted, so callers can supply a fallback message. */
function announce(data: unknown): boolean {
  if (!data || typeof data !== "object") return false;
  const d = data as Record<string, unknown>;
  if (d.ok === false) {
    toast("error", String(d.detail ?? d.error ?? "Operation failed"));
    return true;
  } else if (d.ok === true && "detail" in d) {
    toast("success", String(d.detail));
    return true;
  } else if ("valid" in d) {
    const msg = typeof d.detail === "string" && d.detail ? d.detail : null;
    toast(d.valid ? "success" : "error", msg ?? (d.valid ? "Session is valid" : "Session invalid or expired"));
    return true;
  } else if (d.queued === true) {
    toast("info", "Processing started — the status badge updates here automatically");
    return true;
  } else if (typeof d.error === "string") {
    toast("error", d.error);
    return true;
  }
  return false;
}

function errorMessage(err: unknown): string {
  const e = err as { response?: { data?: { detail?: unknown; message?: unknown; error?: unknown } }; message?: string };
  const rd = e?.response?.data;
  const detail = rd?.detail ?? rd?.message ?? rd?.error ?? e?.message;
  return typeof detail === "string" ? detail : "Request failed";
}

// Polling is the fallback — the WS feed invalidates these keys on every
// event, so intervals stay generous to avoid double-fetch traffic.
export function useOverview(days = 30) {
  return useQuery({ queryKey: ["overview", days], queryFn: () => get(`/analytics/overview?days=${days}`), refetchInterval: 60000 });
}
export function useAccounts() {
  return useQuery({ queryKey: ["accounts"], queryFn: () => get("/accounts"), refetchInterval: 60000 });
}

/** Videos list: poll only while something is uploaded/processing —
 * once everything settles, refetching stops (and so does the log noise). */
export function useVideos(status = "", opts: { limit?: number } = {}) {
  const limit = opts.limit ?? 50;
  const params = new URLSearchParams();
  if (status) params.set("status", status);
  if (limit !== 50) params.set("limit", String(limit));
  const qs = params.toString();
  return useQuery({
    queryKey: ["videos", status, limit],
    queryFn: () => get(`/videos${qs ? `?${qs}` : ""}`),
    refetchInterval: (query) => {
      const rows = (query.state.data ?? []) as { status?: string }[];
      const busy = rows.some((v) => v.status === "uploaded" || v.status === "processing");
      return busy ? 5000 : false;
    },
  });
}
export function usePosts(status = "", opts: { accountId?: number | null; limit?: number } = {}) {
  const limit = opts.limit ?? 50;
  const params = new URLSearchParams();
  if (status) params.set("status", status);
  if (opts.accountId) params.set("account_id", String(opts.accountId));
  if (limit !== 50) params.set("limit", String(limit));
  const qs = params.toString();
  return useQuery({
    queryKey: ["posts", status, opts.accountId ?? "all", limit],
    queryFn: () => get(`/posts${qs ? `?${qs}` : ""}`),
    refetchInterval: 30000,
  });
}
export function useQueue() {
  return useQuery({ queryKey: ["queue"], queryFn: () => get("/posts/queue"), refetchInterval: 30000 });
}
export function useRules() {
  // Beat retires one-shot pins and pauses rules in the background; no WS
  // event covers this key, so poll like the other live lists.
  return useQuery({ queryKey: ["rules"], queryFn: () => get("/schedule"), refetchInterval: 30000 });
}
export function useCaptions() {
  return useQuery({ queryKey: ["captions"], queryFn: () => get("/captions") });
}
export function useHashtags() {
  return useQuery({ queryKey: ["hashtags"], queryFn: () => get("/hashtags") });
}
export function useBios() {
  return useQuery({ queryKey: ["bios"], queryFn: () => get("/bios") });
}
export function useProxies() {
  // No WS-only staleness: the checker lands every ~30 min and realtime can
  // drop, so poll like the other live lists (the pipeline card polls 20s).
  return useQuery({ queryKey: ["proxies"], queryFn: () => get("/proxies"), refetchInterval: 30000 });
}
export function useProxySources() {
  return useQuery({ queryKey: ["proxy-sources"], queryFn: () => get("/proxies/sources") });
}
export function useEffects() {
  return useQuery({ queryKey: ["effects"], queryFn: () => get("/effects") });
}
export function useAudios() {
  return useQuery({ queryKey: ["audio"], queryFn: () => get("/audio") });
}
export function useAudioStats() {
  return useQuery({ queryKey: ["audio-stats"], queryFn: () => get("/analytics/audio") });
}
export function useLogs() {
  return useQuery({ queryKey: ["logs"], queryFn: () => get("/logs?limit=200"), refetchInterval: 30000 });
}
export function useSettings() {
  return useQuery({ queryKey: ["settings"], queryFn: () => get("/settings") });
}
export function useTimezone() {
  // IANA zone the schedule-rule hours are interpreted in (for labels).
  return useQuery({ queryKey: ["timezone"], queryFn: () => get("/system/timezone"), staleTime: 60000 });
}
export function useServerStats() {
  // Live host/container resources; 5s poll is the point of the page.
  // Skipped in background tabs by the browser's interval throttling.
  return useQuery({ queryKey: ["server-stats"], queryFn: () => get("/system/stats"), refetchInterval: 5000 });
}
export function useSystemHealth() {
  // Connectivity health of every pipeline dependency; 15s poll keeps the
  // Health page fresh without hammering the (blocking) worker ping.
  return useQuery({ queryKey: ["system-health"], queryFn: () => get("/system/health"), refetchInterval: 15000 });
}
export function useNotifications() {
  // Bell feed: recent notifications + unread count + upcoming slots.
  // 30s poll — event-driven freshness comes from the watchdog task.
  return useQuery({ queryKey: ["notifications"], queryFn: () => get("/notifications"), refetchInterval: 30000 });
}
export function useMarkNotificationRead() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.post(`/notifications/${id}/read`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["notifications"] }),
    onError: (err) => toast("error", errorMessage(err)),
  });
}
export function useMarkAllNotificationsRead() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.post("/notifications/read-all"),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["notifications"] }),
    onError: (err) => toast("error", errorMessage(err)),
  });
}
export function useBestSlots(accountId: string | number | "") {
  return useQuery({
    queryKey: ["best-slots", accountId],
    queryFn: () => get(`/analytics/best-slots?account_id=${accountId}`),
    enabled: accountId !== "",
  });
}
export function useVideoScore(id: string | undefined, status: string | undefined) {
  // Static per video state — the key includes status so a reprocess refreshes it.
  return useQuery({
    queryKey: ["video-score", id, status], queryFn: () => get(`/videos/${id}/score`),
    enabled: !!id && !!status,
  });
}
export function useApiMutation(method: "post" | "put" | "delete", invalidate: string[][] = [], successMsg?: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ url, body }: { url: string; body?: unknown }) => {
      // Instagram-facing endpoints can take minutes; don't let axios kill the request at 30s.
      // 200s beats the longest backend wait (180s) with margin, still under nginx's 300s.
      const { data } = await api.request({ method, url, data: body, timeout: 200000 });
      return data;
    },
    onSuccess: (data) => {
      // API-shaped payloads announce themselves; otherwise fall back to the
      // caller-supplied message so no successful action stays silent.
      if (!announce(data) && successMsg) toast("success", successMsg);
      for (const key of invalidate) qc.invalidateQueries({ queryKey: key });
      qc.invalidateQueries({ queryKey: ["overview"] });
    },
    onError: (err) => {
      toast("error", errorMessage(err));
    },
  });
}
