"use client";
import {
  Cog, Database, Film, Globe, HardDrive, HeartPulse, Instagram, Radio, RefreshCw, Timer,
} from "lucide-react";
import { Card, CardTitle, QueryFailed, Spinner } from "@/components/ui";
import { IncidentTimeline } from "@/components/dashboard/incident-timeline";
import { useSystemHealth } from "@/hooks/use-api";
import { cn } from "@/lib/utils";

interface Component {
  name: string;
  label: string;
  status: "ok" | "warn" | "down";
  critical: boolean;
  latency_ms: number;
  message: string;
}

const ICONS: Record<string, typeof Database> = {
  database: Database,
  redis: Radio,
  celery_worker: Cog,
  celery_worker_fast: Cog,
  celery_worker_slow: Cog,
  celery_beat: Timer,
  instagram: Instagram,
  proxies: Globe,
  ffmpeg: Film,
  disk: HardDrive,
};

const TONE: Record<Component["status"], { dot: string; pill: string; text: string }> = {
  ok: {
    dot: "bg-emerald-500",
    pill: "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300",
    text: "Operational",
  },
  warn: {
    dot: "bg-amber-500",
    pill: "bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300",
    text: "Degraded",
  },
  down: {
    dot: "bg-red-500",
    pill: "bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300",
    text: "Down",
  },
};

function OverallBanner({ overall }: { overall: string }) {
  const tone = TONE[overall as Component["status"]] ?? TONE.warn;
  return (
    <div className={cn("flex items-center gap-3 rounded-xl border p-4",
      overall === "ok" && "border-emerald-200 bg-emerald-50 dark:border-emerald-900 dark:bg-emerald-950/40",
      overall === "warn" && "border-amber-200 bg-amber-50 dark:border-amber-900 dark:bg-amber-950/40",
      overall === "down" && "border-red-200 bg-red-50 dark:border-red-900 dark:bg-red-950/40",
    )}>
      <span className={cn("h-3 w-3 shrink-0 rounded-full", tone.dot)} />
      <HeartPulse className="h-5 w-5 shrink-0 text-zinc-500" />
      <div className="min-w-0">
        <div className="font-semibold text-zinc-900 dark:text-zinc-100">
          {overall === "ok" && "All systems operational"}
          {overall === "warn" && "Some components need attention"}
          {overall === "down" && "Critical component down"}
        </div>
        <div className="text-sm text-zinc-500">
          {overall === "ok"
            ? "Database, Redis, worker and scheduler are all reachable."
            : overall === "warn"
              ? "Nothing critical is down, but check the degraded components below."
              : "A critical dependency failed — scheduled posting may be affected."}
        </div>
      </div>
    </div>
  );
}

export default function HealthPage() {
  const { data, isLoading, isError, refetch, isFetching, dataUpdatedAt } = useSystemHealth();

  if (isLoading) return <Spinner />;
  if (isError || !data) return <QueryFailed onRetry={() => refetch()} />;

  const components = (data.components ?? []) as Component[];

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-xl font-bold text-zinc-900 dark:text-zinc-100">System Health</h1>
        <button
          onClick={() => refetch()}
          disabled={isFetching}
          className="btn-ghost flex items-center gap-1.5 text-sm"
        >
          <RefreshCw className={cn("h-4 w-4", isFetching && "animate-spin")} />
          Refresh
        </button>
      </div>

      <OverallBanner overall={data.overall ?? "warn"} />

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        {components.map((c) => {
          const Icon = ICONS[c.name] ?? HeartPulse;
          const tone = TONE[c.status] ?? TONE.warn;
          return (
            <Card key={c.name}>
              <div className="flex items-start justify-between gap-2">
                <div className="flex items-center gap-2">
                  <Icon className="h-5 w-5 text-zinc-400" />
                  <CardTitle>{c.label}</CardTitle>
                </div>
                <span className={cn("flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-medium", tone.pill)}>
                  <span className={cn("h-1.5 w-1.5 rounded-full", tone.dot)} />
                  {tone.text}
                </span>
              </div>
              <p className="mt-2 break-words text-sm text-zinc-600 dark:text-zinc-300">{c.message}</p>
              <div className="mt-3 flex items-center justify-between text-xs text-zinc-400">
                <span>{c.latency_ms} ms</span>
                {c.critical && <span className="font-medium">critical</span>}
              </div>
            </Card>
          );
        })}
      </div>

      <p className="text-xs text-zinc-400">
        Auto-refreshes every 15 seconds · Last checked{" "}
        {dataUpdatedAt ? new Date(dataUpdatedAt).toLocaleTimeString() : "—"}
      </p>

      <IncidentTimeline />
    </div>
  );
}
