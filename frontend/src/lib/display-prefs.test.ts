import { describe, expect, it } from "vitest";
import {
  isAccentName,
  moveItem,
  moveWidgetId,
  normalizeWidgetOrder,
} from "./display-prefs";

describe("isAccentName", () => {
  it("accepts the four known accents only", () => {
    expect(isAccentName("emerald")).toBe(true);
    expect(isAccentName("violet")).toBe(true);
    expect(isAccentName("sky")).toBe(true);
    expect(isAccentName("rose")).toBe(true);
    expect(isAccentName("red")).toBe(false);
    expect(isAccentName("")).toBe(false);
    expect(isAccentName(null)).toBe(false);
    expect(isAccentName(undefined)).toBe(false);
  });
});

describe("moveItem", () => {
  it("moves an element forward and backward", () => {
    expect(moveItem(["a", "b", "c", "d"], 0, 2)).toEqual(["b", "c", "a", "d"]);
    expect(moveItem(["a", "b", "c", "d"], 3, 0)).toEqual(["d", "a", "b", "c"]);
  });
  it("returns an equal copy for no-op or out-of-range moves", () => {
    expect(moveItem(["a", "b"], 1, 1)).toEqual(["a", "b"]);
    expect(moveItem(["a", "b"], -1, 0)).toEqual(["a", "b"]);
    expect(moveItem(["a", "b"], 0, 5)).toEqual(["a", "b"]);
    const src = ["a", "b"];
    expect(moveItem(src, 0, 1)).not.toBe(src); // no mutation
    expect(src).toEqual(["a", "b"]);
  });
});

describe("moveWidgetId", () => {
  it("reorders by dragged/target ids", () => {
    expect(moveWidgetId(["a", "b", "c"], "a", "c")).toEqual(["b", "c", "a"]);
    expect(moveWidgetId(["a", "b", "c"], "nope", "a")).toEqual(["a", "b", "c"]);
  });
});

describe("normalizeWidgetOrder", () => {
  const known = ["kpis", "charts", "activity"];
  it("keeps stored order for known ids", () => {
    expect(normalizeWidgetOrder(["charts", "kpis"], known)).toEqual(["charts", "kpis", "activity"]);
  });
  it("drops unknown ids and dedupes", () => {
    expect(normalizeWidgetOrder(["charts", "ghost", "charts", "kpis"], known)).toEqual([
      "charts",
      "kpis",
      "activity",
    ]);
  });
  it("falls back to defaults for garbage input", () => {
    expect(normalizeWidgetOrder(null, known)).toEqual(known);
    expect(normalizeWidgetOrder("nope", known)).toEqual(known);
    expect(normalizeWidgetOrder([], known)).toEqual(known);
  });
});
