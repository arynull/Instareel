/** Pure helpers for the command palette. Tested in palette.test.ts. */

/** Subsequence fuzzy match: "anl" matches "Analytics". Case-insensitive. */
export function fuzzyMatch(query: string, text: string): boolean {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  const t = text.toLowerCase();
  let i = 0;
  for (const ch of t) {
    if (ch === q[i]) i++;
    if (i === q.length) return true;
  }
  return false;
}

/** Filter palette actions by query against label + hint. */
export function filterActions<T extends { label: string; hint?: string }>(actions: T[], query: string): T[] {
  return actions.filter((a) => fuzzyMatch(query, `${a.label} ${a.hint ?? ""}`));
}
