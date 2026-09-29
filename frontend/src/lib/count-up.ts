/** Pure helpers for the animated count-up (testable without a DOM). */

/** easeOutExpo: fast start, gentle landing — the standard easing for counters. */
export function easeOutExpo(t: number): number {
  if (t <= 0) return 0;
  if (t >= 1) return 1;
  return 1 - Math.pow(2, -10 * t);
}

/** Interpolated value at progress p (clamped to [0,1]) between from and to. */
export function countUpValue(from: number, to: number, progress: number): number {
  const p = Math.min(1, Math.max(0, progress));
  return from + (to - from) * easeOutExpo(p);
}
