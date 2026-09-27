export type VideoStatusSnapshot = { status: string; failed_reason?: string | null };

export type ProcessWaitDeps = {
  /** GET /videos/{id} — resolves with the current status snapshot. */
  getStatus: (videoId: number) => Promise<VideoStatusSnapshot>;
  /** POST /videos/{id}/process — best-effort kick when auto-process is off. */
  triggerProcess: (videoId: number) => Promise<void>;
  /** Called with human-readable progress ("Processing video…"). */
  onStep?: (step: string) => void;
  /** True when the caller unmounted — aborts the wait. */
  isCancelled?: () => boolean;
  pollMs?: number;
  timeoutMs?: number;
  /** Grace period before the explicit trigger (lets auto-process pick it up). */
  triggerAfterMs?: number;
};

const DEFAULT_POLL_MS = 3000;
const DEFAULT_TIMEOUT_MS = 10 * 60 * 1000;
const DEFAULT_TRIGGER_AFTER_MS = 8000;

/**
 * Wait until a freshly uploaded video finishes FFmpeg processing.
 * Resolves on "processed". Rejects with a clear message on "failed",
 * on timeout, or when the caller unmounts.
 *
 * Upload only queues processing (async celery task), so scheduling a post
 * immediately after upload always 400s with "Video must be processed first".
 * If the video is still sitting in "uploaded" past the grace period
 * (auto_process_on_upload disabled), this kicks processing explicitly once —
 * a 409 "Already processing" from the trigger is swallowed.
 */
export async function waitForVideoProcessed(videoId: number, deps: ProcessWaitDeps): Promise<void> {
  const pollMs = deps.pollMs ?? DEFAULT_POLL_MS;
  const timeoutMs = deps.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const triggerAfterMs = deps.triggerAfterMs ?? DEFAULT_TRIGGER_AFTER_MS;
  const started = Date.now();
  let triggered = false;

  for (;;) {
    if (deps.isCancelled?.()) throw new Error("cancelled");
    const snap = await deps.getStatus(videoId);
    if (snap.status === "processed") return;
    if (snap.status === "failed") {
      throw new Error(snap.failed_reason?.trim() || "Video processing failed");
    }
    if (!triggered && snap.status === "uploaded" && Date.now() - started > triggerAfterMs) {
      triggered = true;
      try {
        await deps.triggerProcess(videoId);
      } catch (e: unknown) {
        // 409 = the worker already picked it up between our poll and trigger.
        const code = (e as { response?: { status?: number } })?.response?.status;
        if (code !== 409) throw e;
      }
    }
    if (Date.now() - started > timeoutMs) {
      throw new Error(
        "Still processing after 10 minutes — the video stays in your library; schedule it from the Videos page once it finishes.",
      );
    }
    deps.onStep?.("Processing video…");
    await new Promise((r) => setTimeout(r, pollMs));
  }
}
