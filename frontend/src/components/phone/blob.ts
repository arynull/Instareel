"use client";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";

type UrlFactory = {
  create: (blob: Blob) => string;
  revoke: (url: string) => void;
};

const domUrls: UrlFactory = {
  create: (blob) => URL.createObjectURL(blob),
  revoke: (url) => {
    try {
      URL.revokeObjectURL(url);
    } catch {
      /* already revoked / invalid — nothing to do */
    }
  },
};

/** Bounded object-URL cache: one authenticated fetch per media, shared by
 * grid cells and the player. Evicts the least-recently-added entry past the
 * cap (Map preserves insertion order) and revokes its object URL so long
 * sessions don't leak memory. In-flight requests are de-duplicated. */
export class MediaUrlCache {
  private cache = new Map<string, string>();
  private pending = new Map<string, Promise<string>>();

  constructor(
    private maxEntries = 150,
    private urls: UrlFactory = domUrls,
  ) {}

  get size(): number {
    return this.cache.size;
  }

  /** For tests: does this key currently hold a live object URL? */
  has(key: string): boolean {
    return this.cache.has(key);
  }

  load(key: string, fetchBlob: () => Promise<Blob>): Promise<string> {
    const hit = this.cache.get(key);
    if (hit) return Promise.resolve(hit);
    const inflight = this.pending.get(key);
    if (inflight) return inflight;
    const p = fetchBlob()
      .then((blob) => {
        const objectUrl = this.urls.create(blob);
        this.evictIfNeeded();
        this.cache.set(key, objectUrl);
        this.pending.delete(key);
        return objectUrl;
      })
      .catch((e: unknown) => {
        this.pending.delete(key);
        throw e;
      });
    this.pending.set(key, p);
    return p;
  }

  private evictIfNeeded(): void {
    while (this.cache.size >= this.maxEntries) {
      const oldest = this.cache.keys().next();
      if (oldest.done) break;
      const url = this.cache.get(oldest.value);
      this.cache.delete(oldest.value);
      if (url) this.urls.revoke(url);
    }
  }
}

// Process-scoped singleton used by the hook below.
const sharedCache = new MediaUrlCache();

/** Authenticated media for <img>/<video> tags (preview or thumbnail).
 * Tri-state: url set = ready, failed = gave up (show placeholder, no spin),
 * neither = still loading. */
export function useBlobUrl(kind: "preview" | "thumbnail", id: number | null): { url: string | null; failed: boolean } {
  const [url, setUrl] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    if (!id) {
      setUrl(null);
      return;
    }
    let cancelled = false;
    setUrl(null);
    setFailed(false);
    sharedCache
      .load(`${kind}:${id}`, () =>
        api.get(`/videos/${id}/${kind}`, { responseType: "blob", timeout: 120000 }).then((res) => res.data as Blob),
      )
      .then(
        (u) => {
          if (!cancelled) setUrl(u);
        },
        () => {
          if (!cancelled) setFailed(true);
        },
      );
    return () => {
      cancelled = true;
    };
  }, [kind, id]);
  return { url, failed };
}
