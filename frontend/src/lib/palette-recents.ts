/** Recently used command-palette entries (pure storage helpers). */

const KEY = "igf:palette-recents";
const MAX = 5;

function readRaw(): string[] {
  try {
    const raw = typeof localStorage !== "undefined" ? localStorage.getItem(KEY) : null;
    const arr = JSON.parse(raw ?? "[]");
    return Array.isArray(arr) ? arr.filter((x): x is string => typeof x === "string") : [];
  } catch {
    return [];
  }
}

/** Most-recently-used action ids, newest first. */
export function loadRecentIds(): string[] {
  return readRaw().slice(0, MAX);
}

/** Record an action use; returns the updated id list (newest first, max 5). */
export function recordRecentId(id: string): string[] {
  const ids = [id, ...readRaw().filter((x) => x !== id)].slice(0, MAX);
  try {
    localStorage.setItem(KEY, JSON.stringify(ids));
  } catch {
    /* ignore */
  }
  return ids;
}
