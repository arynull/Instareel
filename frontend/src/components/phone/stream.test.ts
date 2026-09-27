import { describe, expect, it } from "vitest";
import { previewStreamUrl } from "./stream";

describe("previewStreamUrl", () => {
  it("builds the same-origin preview URL with the token", () => {
    expect(previewStreamUrl(7, "abc123")).toBe("/api/v1/videos/7/preview?token=abc123");
  });

  it("URL-encodes the token", () => {
    const url = previewStreamUrl(7, "a+b/c=d&e");
    expect(url).toBe("/api/v1/videos/7/preview?token=a%2Bb%2Fc%3Dd%26e");
    // The raw token must not leak unescaped into the query string.
    expect(url).not.toContain("a+b");
  });
});
