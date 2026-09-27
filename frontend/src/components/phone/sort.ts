/** Recency ordering for posts: posted_at first, then created_at.
 * Null-safe — posts missing both timestamps sort last instead of throwing
 * (the old inline `(b.posted_at ?? b.created_at).localeCompare(...)` crashed
 * with a TypeError when both were null). */
export function comparePostRecency(
  a: { posted_at?: string | null; created_at?: string | null },
  b: { posted_at?: string | null; created_at?: string | null },
): number {
  const ka = a.posted_at ?? a.created_at ?? "";
  const kb = b.posted_at ?? b.created_at ?? "";
  if (ka === kb) return 0;
  return ka < kb ? 1 : -1;
}
