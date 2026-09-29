import { describe, expect, it } from "vitest";
import {
  bestCells,
  cellLabel,
  funnelConversions,
  funnelWidths,
  heatIntensity,
} from "./dashboard";

describe("funnelConversions", () => {
  it("computes stage-to-stage percentages, first stage null", () => {
    const stages = [
      { key: "a", label: "A", count: 100 },
      { key: "b", label: "B", count: 50 },
      { key: "c", label: "C", count: 25 },
    ];
    expect(funnelConversions(stages)).toEqual([null, 50, 50]);
  });
  it("returns null (not 0% or NaN) when the previous stage is empty", () => {
    const stages = [
      { key: "a", label: "A", count: 0 },
      { key: "b", label: "B", count: 10 },
    ];
    expect(funnelConversions(stages)).toEqual([null, null]);
  });
  it("rounds to one decimal", () => {
    const stages = [
      { key: "a", label: "A", count: 3 },
      { key: "b", label: "B", count: 1 },
    ];
    expect(funnelConversions(stages)).toEqual([null, 33.3]);
  });
});

describe("funnelWidths", () => {
  it("scales bars relative to the widest stage with a minimum width", () => {
    const stages = [
      { key: "a", label: "A", count: 100 },
      { key: "b", label: "B", count: 0 },
    ];
    expect(funnelWidths(stages)).toEqual([100, 4]);
  });
  it("handles all-zero stages without NaN", () => {
    const stages = [{ key: "a", label: "A", count: 0 }];
    expect(funnelWidths(stages)).toEqual([4]);
  });
});

describe("heatIntensity", () => {
  it("is 0 for empty or missing data", () => {
    expect(heatIntensity(0, 100)).toBe(0);
    expect(heatIntensity(50, 0)).toBe(0);
  });
  it("is 1 at the max and sqrt-scaled below it", () => {
    expect(heatIntensity(100, 100)).toBe(1);
    expect(heatIntensity(25, 100)).toBeCloseTo(0.5);
  });
  it("never exceeds 1", () => {
    expect(heatIntensity(500, 100)).toBe(1);
  });
});

describe("bestCells", () => {
  const cells = [
    { dow: 0, hour: 9, posts: 2, avg_views: 100 },
    { dow: 1, hour: 18, posts: 5, avg_views: 900 },
    { dow: 2, hour: 12, posts: 0, avg_views: 0 },
    { dow: 3, hour: 20, posts: 1, avg_views: 500 },
  ];
  it("returns top n by avg_views, skipping empty cells", () => {
    const top = bestCells(cells, 2);
    expect(top.map((c) => c.hour)).toEqual([18, 20]);
  });
  it("ties break by post count", () => {
    const tied = [
      { dow: 0, hour: 1, posts: 1, avg_views: 100 },
      { dow: 0, hour: 2, posts: 9, avg_views: 100 },
    ];
    expect(bestCells(tied, 2)[0].hour).toBe(2);
  });
});

describe("cellLabel", () => {
  it("formats day + zero-padded hour", () => {
    expect(cellLabel({ dow: 0, hour: 9, posts: 1, avg_views: 1 }, ["Mon"])).toBe("Mon 09:00");
  });
});
