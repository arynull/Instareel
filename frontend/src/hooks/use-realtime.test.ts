import { describe, expect, it } from "vitest";
import { REALTIME_QUERY_KEYS, queryKeysForEvent } from "./use-realtime";

describe("queryKeysForEvent", () => {
  it("maps the new push events to their queries", () => {
    expect(queryKeysForEvent("notification")).toEqual(["notifications"]);
    expect(queryKeysForEvent("analytics_update")).toEqual(["overview", "funnel"]);
    expect(queryKeysForEvent("schedule_update")).toEqual(["rules"]);
  });

  it("keeps the pre-existing mappings intact", () => {
    expect(queryKeysForEvent("post_status_update")).toEqual(["posts", "queue", "overview"]);
    expect(queryKeysForEvent("video_processing_complete")).toEqual(["videos", "video-sources", "overview"]);
    expect(queryKeysForEvent("video_processing_progress")).toEqual(["videos", "video-sources", "overview"]);
    expect(queryKeysForEvent("video_source_update")).toEqual(["videos", "video-sources", "overview"]);
    expect(queryKeysForEvent("account_status_change")).toEqual(["accounts"]);
    expect(queryKeysForEvent("new_log")).toEqual(["logs"]);
    expect(queryKeysForEvent("proxy_pool_update")).toEqual(["proxies", "proxy-pipeline", "proxy-sources"]);
  });

  it("ignores unknown events and control frames", () => {
    expect(queryKeysForEvent("ping")).toEqual([]);
    expect(queryKeysForEvent("connected")).toEqual([]);
    expect(queryKeysForEvent("something_new")).toEqual([]);
    expect(queryKeysForEvent("")).toEqual([]);
  });
});

describe("REALTIME_QUERY_KEYS", () => {
  it("covers every key any event can invalidate (reconnect refetch is complete)", () => {
    const mapped = new Set<string>();
    for (const ev of [
      "video_processing_progress",
      "video_processing_complete",
      "video_source_update",
      "post_status_update",
      "account_status_change",
      "new_log",
      "proxy_pool_update",
      "notification",
      "analytics_update",
      "schedule_update",
    ]) {
      for (const k of queryKeysForEvent(ev)) mapped.add(k);
    }
    for (const k of mapped) {
      expect(REALTIME_QUERY_KEYS).toContain(k);
    }
  });

  it("has no duplicates", () => {
    expect(new Set(REALTIME_QUERY_KEYS).size).toBe(REALTIME_QUERY_KEYS.length);
  });
});
