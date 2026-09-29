"use client";
import type { ReactNode } from "react";

/** Shared vertical gradient defs for a chart series — `id` must be unique per chart. */
export function ChartGradient({ id, color }: { id: string; color: string }) {
  return (
    <defs>
      <linearGradient id={id} x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stopColor={color} stopOpacity={0.32} />
        <stop offset="100%" stopColor={color} stopOpacity={0} />
      </linearGradient>
    </defs>
  );
}

interface TooltipEntry {
  name?: ReactNode;
  value?: number | string;
  color?: string;
  dataKey?: string | number;
}

/**
 * Glassmorphism tooltip for Recharts — pass as content={<ChartTooltip />}.
 * `format` customizes the value cell, e.g. (v) => fmt(Number(v)).
 */
export function ChartTooltip({
  active,
  payload,
  label,
  format,
}: {
  active?: boolean;
  payload?: TooltipEntry[];
  label?: string | number;
  format?: (value: number | string, name: string) => ReactNode;
}) {
  if (!active || !payload || payload.length === 0) return null;
  return (
    <div className="rounded-xl border border-zinc-200/70 bg-white/85 px-3 py-2 shadow-xl backdrop-blur-md dark:border-zinc-700/60 dark:bg-zinc-900/85">
      {label != null && label !== "" && (
        <p className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-zinc-400">
          {label}
        </p>
      )}
      <div className="space-y-0.5">
        {payload.map((e, i) => (
          <div key={i} className="flex items-center gap-2 text-xs">
            <span
              className="h-2 w-2 shrink-0 rounded-full"
              style={{ background: e.color ?? "#10b981" }}
            />
            <span className="text-zinc-500 dark:text-zinc-400">{e.name}</span>
            <span className="tnum ml-auto pl-3 font-semibold text-zinc-900 dark:text-zinc-100">
              {format ? format(e.value ?? "", String(e.name ?? "")) : e.value}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
