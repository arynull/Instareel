import { api, apiBase } from "@/lib/api";

/** Direct, streamable <video> src for a processed video.
 * The backend signs a short-lived token because <video> tags can't send
 * Authorization headers; the browser then streams with Range requests
 * instead of us blob-downloading the whole file up front. */
export function previewStreamUrl(videoId: number, token: string): string {
  return `${apiBase()}/api/v1/videos/${videoId}/preview?token=${encodeURIComponent(token)}`;
}

/** Fetch a fresh playback token for one video. Tokens are short-lived, so
 * callers should fetch lazily (when the reel scrolls into view), not up front. */
export async function fetchPreviewToken(videoId: number): Promise<string> {
  const { data } = await api.get(`/videos/${videoId}/preview-token`);
  const token = (data as { token?: unknown }).token;
  if (typeof token !== "string" || token.length === 0) {
    throw new Error("preview token missing in response");
  }
  return token;
}
