/** Pure display-preference logic: accents, density, widget ordering.
 *  Storage access is guarded so these are safe to call during SSR/prerender. */

export const ACCENTS = {
  emerald: { brand: "#10b981", brandHover: "#059669", label: "Emerald" },
  violet: { brand: "#8b5cf6", brandHover: "#7c3aed", label: "Violet" },
  sky: { brand: "#0ea5e9", brandHover: "#0284c7", label: "Sky" },
  rose: { brand: "#f43f5e", brandHover: "#e11d48", label: "Rose" },
} as const;

export type AccentName = keyof typeof ACCENTS;
export type Density = "comfortable" | "compact";

const ACCENT_KEY = "igf:accent";
const DENSITY_KEY = "igf:density";
export const WIDGET_ORDER_KEY = "igf:dashboard-widgets";

export function isAccentName(v: unknown): v is AccentName {
  return typeof v === "string" && v in ACCENTS;
}

function readKey(key: string): string | null {
  try {
    return typeof localStorage !== "undefined" ? localStorage.getItem(key) : null;
  } catch {
    return null;
  }
}

function writeKey(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* private mode etc. — prefs just don't persist */
  }
}

export function readAccent(): AccentName {
  const v = readKey(ACCENT_KEY);
  return isAccentName(v) ? v : "emerald";
}

export function writeAccent(a: AccentName): void {
  writeKey(ACCENT_KEY, a);
}

export function readDensity(): Density {
  return readKey(DENSITY_KEY) === "compact" ? "compact" : "comfortable";
}

export function writeDensity(d: Density): void {
  writeKey(DENSITY_KEY, d);
}

/** Move an element inside an array — pure, used by widget drag-and-drop. */
export function moveItem<T>(arr: readonly T[], from: number, to: number): T[] {
  const next = [...arr];
  if (from < 0 || from >= next.length || to < 0 || to >= next.length || from === to) {
    return next;
  }
  const [item] = next.splice(from, 1);
  next.splice(to, 0, item);
  return next;
}

/** Move a widget id before/after another id inside an order list. */
export function moveWidgetId(order: readonly string[], dragId: string, targetId: string): string[] {
  return moveItem(order, order.indexOf(dragId), order.indexOf(targetId));
}

/** Normalize a stored widget order: keep known ids in stored order,
 *  append any new ids at the end, drop unknown ids. */
export function normalizeWidgetOrder(stored: unknown, known: readonly string[]): string[] {
  const arr = Array.isArray(stored) ? stored.filter((x): x is string => typeof x === "string") : [];
  const seen = new Set<string>();
  const out: string[] = [];
  for (const id of arr) {
    if (known.includes(id) && !seen.has(id)) {
      seen.add(id);
      out.push(id);
    }
  }
  for (const id of known) {
    if (!seen.has(id)) out.push(id);
  }
  return out;
}

export function readWidgetOrder(known: readonly string[]): string[] {
  const raw = readKey(WIDGET_ORDER_KEY);
  if (!raw) return [...known];
  try {
    return normalizeWidgetOrder(JSON.parse(raw), known);
  } catch {
    return [...known];
  }
}

export function writeWidgetOrder(order: readonly string[]): void {
  writeKey(WIDGET_ORDER_KEY, JSON.stringify(order));
}
