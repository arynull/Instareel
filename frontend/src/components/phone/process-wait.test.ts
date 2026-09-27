import { describe, expect, it, vi } from "vitest";
import { waitForVideoProcessed, type ProcessWaitDeps } from "./process-wait";

type TestDeps = ProcessWaitDeps & { getStatus: any; triggerProcess: any };

function deps(overrides: Partial<ProcessWaitDeps> = {}): TestDeps {
  const base: TestDeps = {
    getStatus: vi.fn(async () => ({ status: "processed" })),
    triggerProcess: vi.fn(async () => {}),
    pollMs: 5,
    timeoutMs: 1000,
    triggerAfterMs: 30,
  };
  return { ...base, ...overrides };
}

describe("waitForVideoProcessed", () => {
  it("resolves immediately when the video is already processed", async () => {
    const d = deps();
    await waitForVideoProcessed(1, d);
    expect(d.getStatus).toHaveBeenCalledTimes(1);
  });

  it("polls until processed", async () => {
    const statuses = [{ status: "processing" }, { status: "processing" }, { status: "processed" }];
    const d = deps({ getStatus: vi.fn(async () => statuses.shift()!) });
    await waitForVideoProcessed(1, d);
    expect(d.getStatus).toHaveBeenCalledTimes(3);
  });

  it("rejects with the backend's failed_reason on failure", async () => {
    const d = deps({ getStatus: vi.fn(async () => ({ status: "failed", failed_reason: "ffmpeg exploded" })) });
    await expect(waitForVideoProcessed(1, d)).rejects.toThrow("ffmpeg exploded");
  });

  it("rejects with a generic message when failed_reason is empty", async () => {
    const d = deps({ getStatus: vi.fn(async () => ({ status: "failed", failed_reason: "  " })) });
    await expect(waitForVideoProcessed(1, d)).rejects.toThrow("Video processing failed");
  });

  it("kicks processing explicitly when stuck in uploaded (auto-process off)", async () => {
    const d = deps({
      getStatus: vi.fn(async () => ({ status: "uploaded" })),
      triggerAfterMs: 0,
      timeoutMs: 60,
      pollMs: 5,
    });
    // Never reaches processed -> times out, but the trigger must have fired once.
    await expect(waitForVideoProcessed(1, d)).rejects.toThrow("Still processing after 10 minutes");
    expect(d.triggerProcess).toHaveBeenCalledTimes(1);
    expect(d.triggerProcess).toHaveBeenCalledWith(1);
  });

  it("swallows a 409 from the trigger (worker picked it up in between)", async () => {
    const conflict = Object.assign(new Error("conflict"), { response: { status: 409 } });
    // Several "uploaded" polls so the trigger grace period reliably elapses.
    const statuses = [{ status: "uploaded" }, { status: "uploaded" }, { status: "uploaded" }, { status: "processed" }];
    const d = deps({
      getStatus: vi.fn(async () => statuses.shift()!),
      triggerProcess: vi.fn(async () => {
        throw conflict;
      }),
      triggerAfterMs: 0,
    });
    await waitForVideoProcessed(1, d);
    expect(d.triggerProcess).toHaveBeenCalledTimes(1);
  });

  it("rethrows non-409 trigger errors", async () => {
    const boom = Object.assign(new Error("queue down"), { response: { status: 503 } });
    const d = deps({
      getStatus: vi.fn(async () => ({ status: "uploaded" })),
      triggerProcess: vi.fn(async () => {
        throw boom;
      }),
      triggerAfterMs: 0,
    });
    await expect(waitForVideoProcessed(1, d)).rejects.toThrow("queue down");
  });

  it("reports progress via onStep while waiting", async () => {
    const statuses = [{ status: "processing" }, { status: "processed" }];
    const onStep = vi.fn();
    const d = deps({ getStatus: vi.fn(async () => statuses.shift()!), onStep });
    await waitForVideoProcessed(1, d);
    expect(onStep).toHaveBeenCalledWith("Processing video…");
  });

  it("aborts when the caller unmounts", async () => {
    const d = deps({
      getStatus: vi.fn(async () => ({ status: "processing" })),
      isCancelled: () => true,
    });
    await expect(waitForVideoProcessed(1, d)).rejects.toThrow("cancelled");
  });
});
