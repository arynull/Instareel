import { describe, expect, it } from "vitest";
import { nextTheme } from "./theme";

describe("nextTheme", () => {
  it("cycles light -> dark -> black -> light", () => {
    expect(nextTheme("light")).toBe("dark");
    expect(nextTheme("dark")).toBe("black");
    expect(nextTheme("black")).toBe("light");
  });
  it("restarts the cycle for unknown input", () => {
    expect(nextTheme("system")).toBe("light");
    expect(nextTheme("")).toBe("light");
  });
});
