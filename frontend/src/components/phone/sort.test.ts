import { describe, expect, it } from "vitest";
import { comparePostRecency } from "./sort";

describe("comparePostRecency", () => {
  it("orders newest first by posted_at", () => {
    const a = { posted_at: "2026-09-20T10:00:00", created_at: "2026-09-19T10:00:00" };
    const b = { posted_at: "2026-09-21T10:00:00", created_at: "2026-09-19T10:00:00" };
    expect([a, b].sort(comparePostRecency)).toEqual([b, a]);
  });

  it("falls back to created_at when posted_at is null (scheduled posts)", () => {
    const a = { posted_at: null, created_at: "2026-09-20T10:00:00" };
    const b = { posted_at: null, created_at: "2026-09-21T10:00:00" };
    expect([a, b].sort(comparePostRecency)).toEqual([b, a]);
  });

  it("does not throw when both timestamps are missing — sorts them last", () => {
    const good = { posted_at: "2026-09-21T10:00:00", created_at: null };
    const bad = { posted_at: null, created_at: null };
    expect(() => [good, bad].sort(comparePostRecency)).not.toThrow();
    expect([bad, good].sort(comparePostRecency)[0]).toBe(good);
  });

  it("treats equal timestamps as equal (stable, no crash)", () => {
    const a = { posted_at: null, created_at: null };
    const b = { posted_at: null, created_at: null };
    expect(comparePostRecency(a, b)).toBe(0);
  });
});
