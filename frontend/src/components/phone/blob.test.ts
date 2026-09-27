import { describe, expect, it, vi } from "vitest";
import { MediaUrlCache } from "./blob";

function makeCache(maxEntries = 3) {
  const revoked: string[] = [];
  let n = 0;
  const cache = new MediaUrlCache(maxEntries, {
    create: () => `blob:${++n}`,
    revoke: (url) => {
      revoked.push(url);
    },
  });
  return { cache, revoked };
}

const blob = () => Promise.resolve(new Blob(["x"]));

describe("MediaUrlCache", () => {
  it("fetches once per key and serves the cached URL afterwards", async () => {
    const { cache } = makeCache();
    const fetchBlob = vi.fn(blob);
    const first = await cache.load("thumbnail:1", fetchBlob);
    const second = await cache.load("thumbnail:1", fetchBlob);
    expect(first).toBe(second);
    expect(fetchBlob).toHaveBeenCalledTimes(1);
    expect(cache.size).toBe(1);
  });

  it("de-duplicates in-flight requests for the same key", async () => {
    const { cache } = makeCache();
    const fetchBlob = vi.fn(blob);
    const [a, b] = await Promise.all([cache.load("preview:2", fetchBlob), cache.load("preview:2", fetchBlob)]);
    expect(a).toBe(b);
    expect(fetchBlob).toHaveBeenCalledTimes(1);
  });

  it("evicts the oldest entry past the cap and revokes its object URL", async () => {
    const { cache, revoked } = makeCache(2);
    await cache.load("k1", blob); // blob:1
    await cache.load("k2", blob); // blob:2
    expect(cache.size).toBe(2);
    await cache.load("k3", blob); // evicts k1
    expect(cache.size).toBe(2);
    expect(cache.has("k1")).toBe(false);
    expect(cache.has("k2")).toBe(true);
    expect(cache.has("k3")).toBe(true);
    expect(revoked).toEqual(["blob:1"]);
  });

  it("a re-load after eviction fetches fresh (no dangling revoked URL)", async () => {
    const { cache } = makeCache(1);
    const first = await cache.load("k1", blob);
    await cache.load("k2", blob); // evicts k1, revokes first
    const refetched = await cache.load("k1", blob);
    expect(refetched).not.toBe(first);
  });

  it("clears the in-flight slot on failure so a retry re-fetches", async () => {
    const { cache } = makeCache();
    const failing = vi.fn(() => Promise.reject(new Error("boom")));
    await expect(cache.load("k9", failing)).rejects.toThrow("boom");
    const ok = vi.fn(blob);
    const url = await cache.load("k9", ok);
    expect(ok).toHaveBeenCalledTimes(1);
    expect(url).toMatch(/^blob:/);
  });
});
