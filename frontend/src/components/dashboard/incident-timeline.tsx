"use client";
import { History } from "lucide-react";
import { Card, CardTitle } from "@/components/ui";
import { LiveTimeAgo } from "@/components/live-time";
import { useNotifications } from "@/hooks/use-api";
import { cn } from "@/lib/utils";

/** Notification types that represent infrastructure incidents. */
export const INCIDENT_TYPES = new Set([
  "component_down",
  "component_recovered",
  "scheduler_gap",
  "scheduler_busy",
  "tick_error",
  "analytics_error",
  "proxy_pool_down",
  "possible_shadowban",
  "shadowban_recovered",
  "action_blocked",
  "account_session_invalid",
]);

const SEV_DOT: Record<string, string> = {
  critical: "bg-red-500",
  warning: "bg-amber-500",
  info: "bg-sky-500",
  success: "bg-emerald-500",
};

interface Incident {
  id: number;
  type: string;
  severity: string;
  title: string;
  message: string;
  created_at: string | null;
}

/** Vertical incident history for the System Health page, built from the
 *  existing notifications feed — no new backend endpoint needed. */
export function IncidentTimeline() {
  const { data } = useNotifications();
  const incidents = ((data?.notifications ?? []) as Incident[])
    .filter((n) => INCIDENT_TYPES.has(n.type))
    .slice(0, 15);

  return (
    <Card>
      <CardTitle>Incident history</CardTitle>
      {incidents.length === 0 ? (
        <div className="flex items-center gap-3 py-4 text-sm text-zinc-500">
          <History className="h-5 w-5 shrink-0 text-zinc-300 dark:text-zinc-600" />
          <p>No incidents recorded — components have been stable.</p>
        </div>
      ) : (
        <ol className="relative space-y-4 before:absolute before:bottom-2 before:left-[5px] before:top-2 before:w-px before:bg-zinc-200 dark:before:bg-zinc-800">
          {incidents.map((n) => (
            <li key={n.id} className="relative pl-6">
              <span
                className={cn(
                  "absolute left-0 top-1.5 h-[11px] w-[11px] rounded-full ring-4 ring-white dark:ring-zinc-900",
                  SEV_DOT[n.severity] ?? "bg-zinc-400"
                )}
                aria-hidden
              />
              <div className="flex flex-wrap items-baseline gap-x-2">
                <p className="text-sm font-semibold">{n.title}</p>
                <span className="text-[11px] text-zinc-400">
                  <LiveTimeAgo iso={n.created_at} />
                </span>
              </div>
              <p className="mt-0.5 line-clamp-2 text-xs text-zinc-500" title={n.message}>
                {n.message}
              </p>
            </li>
          ))}
        </ol>
      )}
      <p className="mt-3 text-[11px] text-zinc-400">
        Showing the last {incidents.length} infrastructure event{incidents.length === 1 ? "" : "s"}.
      </p>
    </Card>
  );
}
