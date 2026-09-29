/** Pure helpers for the theme system. Tested in theme.test.ts. */

export const THEME_ORDER = ["light", "dark", "black"] as const;
export type ThemeName = (typeof THEME_ORDER)[number];

/** Next theme in the light → dark → black cycle. Unknown input restarts the cycle at light. */
export function nextTheme(current: string): ThemeName {
  const idx = (THEME_ORDER as readonly string[]).indexOf(current);
  return THEME_ORDER[(idx + 1) % THEME_ORDER.length];
}
