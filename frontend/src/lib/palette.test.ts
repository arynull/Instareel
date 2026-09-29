import { describe, expect, it } from "vitest";
import { filterActions, fuzzyMatch } from "./palette";

describe("fuzzyMatch", () => {
  it("matches empty query against anything", () => {
    expect(fuzzyMatch("", "Analytics")).toBe(true);
    expect(fuzzyMatch("   ", "Analytics")).toBe(true);
  });
  it("matches subsequences case-insensitively", () => {
    expect(fuzzyMatch("anl", "Analytics")).toBe(true);
    expect(fuzzyMatch("ANL", "analytics")).toBe(true);
    expect(fuzzyMatch("hlth", "Go to Health")).toBe(true);
  });
  it("rejects out-of-order characters", () => {
    expect(fuzzyMatch("lna", "Analytics")).toBe(false);
    expect(fuzzyMatch("zzz", "Analytics")).toBe(false);
  });
  it("requires the full query to be consumed", () => {
    expect(fuzzyMatch("analyticsx", "Analytics")).toBe(false);
  });
});

describe("filterActions", () => {
  const actions = [
    { label: "Go to Analytics", hint: "/dashboard/analytics" },
    { label: "Go to Health", hint: "/dashboard/health" },
    { label: "Toggle theme" },
  ];
  it("matches against label and hint", () => {
    expect(filterActions(actions, "health")).toHaveLength(1);
    expect(filterActions(actions, "/dashboard")).toHaveLength(2);
    expect(filterActions(actions, "theme")).toHaveLength(1);
  });
  it("returns everything on empty query", () => {
    expect(filterActions(actions, "")).toHaveLength(3);
  });
});
