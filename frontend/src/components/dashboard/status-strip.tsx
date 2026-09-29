"use client";
import Link from "next/link";
import { ArrowRight, Cog, Timer } from "lucide-react";
import { useOverview, useSystemHealth } from "@/hooks/use-api";
import { fmt } from "@/lib/utils";
import { cn } from "@/lib/utils";

interface LaneState {
  key: string;
  label: string;
  icon: typeof Cog;
  status: "ok" | "warn" | "down" | "unknown";
}

const DOT: Record<LaneState["status"], string> = {
  ok: "bg-emerald-500",
  warn: "bg-amber-500",
  down: "bg-red-500",
  unknown: "bg-zinc-400",
};

/** Slim live-status strip: worker lanes + queue, linking to /health. */
export function StatusStrip() {
  const { data: health } = useSystemHealth();
  const { data: overview } = useOverview(30);

  const components = ((health as any)?.components ?? []) as { name: string; status: string }[];
  const find = (name: string) => components.find((c) => c.name === name)?.status as LaneState["status"] | undefined;

  const lanes: LaneState[] = [
    { key: "fast", label: "Fast lane", icon: Cog, status: find("celery_worker_fast") ?? "unknown" },
    { key: "slow", label: "Slow lane", icon: Cog, status: find("celery_worker_slow") ?? "unknown" },
    { key: "beat", label: "Scheduler", icon: Timer, status: find("celery_beat") ?? "unknown" },
  ];
  const queueSize = (overview as any)?.queue_size ?? 0;
  const scheduled = (overview as any)?.scheduled_count ?? 0;

  return (
    <Link
      href="/dashboard/health"
      className="card group flex flex-wrap items-center gap-x-5 gap-y-2 !py-3 transition hover:border-zinc-300 dark:hover:border-zinc-700"
    >
      {lanes.map(({ key, label, icon: Icon, status }) => (
        <span key={key} className="inline-flex items-center gap-2 text-xs font-medium text-zinc-600 dark:text-zinc-300">
          <span className={cn("live-dot h-2 w-2 rounded-full", DOT[status])} />
          <Icon className="h-3.5 w-3.5 text-zinc-400" />
          {label}
        </span>
      ))}
      <span className="tnum ml-auto text-xs text-zinc-500">
        <strong className="text-zinc-900 dark:text-zinc-100">{fmt(queueSize)}</strong> in queue
        {" · "}
        <strong className="text-zinc-900 dark:text-zinc-100">{fmt(scheduled)}</strong> scheduled
      </span>
      <ArrowRight className="h-3.5 w-3.5 text-zinc-400 transition-transform group-hover:translate-x-0.5" />
    </Link>
  );
}
