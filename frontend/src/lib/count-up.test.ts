import { describe, expect, it } from "vitest";
import { countUpValue, easeOutExpo } from "./count-up";

describe("easeOutExpo", () => {
  it("clamps below 0 to 0 and above 1 to 1", () => {
    expect(easeOutExpo(-0.5)).toBe(0);
    expect(easeOutExpo(0)).toBe(0);
    expect(easeOutExpo(1)).toBe(1);
    expect(easeOutExpo(1.5)).toBe(1);
  });
  it("is monotonically increasing and lands exactly on 1", () => {
    let prev = 0;
    for (let t = 0.1; t < 1; t += 0.1) {
      const v = easeOutExpo(t);
      expect(v).toBeGreaterThan(prev);
      prev = v;
    }
    expect(easeOutExpo(0.999)).toBeLessThan(1);
  });
});

describe("countUpValue", () => {
  it("starts at `from` and ends exactly at `to`", () => {
    expect(countUpValue(0, 100, 0)).toBe(0);
    expect(countUpValue(0, 100, 1)).toBe(100);
    expect(countUpValue(50, 150, 1)).toBe(150);
  });
  it("clamps progress outside [0,1]", () => {
    expect(countUpValue(0, 100, -2)).toBe(0);
    expect(countUpValue(0, 100, 5)).toBe(100);
  });
  it("handles counting down", () => {
    expect(countUpValue(100, 0, 1)).toBe(0);
    const mid = countUpValue(100, 0, 0.5);
    expect(mid).toBeGreaterThan(0);
    expect(mid).toBeLessThan(100);
  });
  it("is exact for zero distance", () => {
    expect(countUpValue(42, 42, 0.37)).toBe(42);
  });
});
