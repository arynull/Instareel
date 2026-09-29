/** Pure helpers for the dashboard command-center widgets. Tested in dashboard.test.ts. */

export interface FunnelStage {
  key: string;
  label: string;
  count: number;
}

/** Conversion % from each stage to the next. `null` when the previous
 *  stage is empty (avoids divide-by-zero and misleading 0%). */
export function funnelConversions(stages: FunnelStage[]): (number | null)[] {
  return stages.map((s, i) => {
    if (i === 0) return null;
    const prev = stages[i - 1].count;
    if (prev <= 0) return null;
    return Math.round((s.count / prev) * 1000) / 10;
  });
}

/** Width % of each funnel bar relative to the widest stage. */
export function funnelWidths(stages: FunnelStage[]): number[] {
  const max = Math.max(1, ...stages.map((s) => s.count));
  return stages.map((s) => Math.max(4, Math.round((s.count / max) * 100)));
}

export interface HeatCell {
  dow: number;
  hour: number;
  posts: number;
  avg_views: number;
}

/** 0..1 intensity for a heatmap cell, sqrt-scaled so a single viral
 *  post doesn't wash out the rest of the grid. */
export function heatIntensity(avgViews: number, maxAvgViews: number): number {
  if (maxAvgViews <= 0 || avgViews <= 0) return 0;
  return Math.min(1, Math.sqrt(avgViews / maxAvgViews));
}

/** Top n cells by avg_views that actually have posts. */
export function bestCells(cells: HeatCell[], n: number): HeatCell[] {
  return cells
    .filter((c) => c.posts > 0)
    .sort((a, b) => b.avg_views - a.avg_views || b.posts - a.posts)
    .slice(0, n);
}

/** "Mon 18:00" style label for a cell. */
export function cellLabel(cell: HeatCell, dowLabels: string[]): string {
  const day = dowLabels[cell.dow] ?? `d${cell.dow}`;
  const hh = String(cell.hour).padStart(2, "0");
  return `${day} ${hh}:00`;
}
