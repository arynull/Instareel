import type { ReactNode } from "react";
import { Inbox } from "lucide-react";
import { cn } from "@/lib/utils";

/** Statuses that represent work in progress — rendered with a pulsing dot. */
const LIVE_STATUSES = new Set(["posting", "processing", "uploading", "downloading", "running", "stopping"]);

export function Card({ children, className }: { children: ReactNode; className?: string }) {
  // min-w-0: lets the card shrink inside grids/flex instead of stretching
  // the page when a child (filename, URL, error) is wider than the viewport.
  return <div className={cn("card min-w-0 p-4 sm:p-5", className)}>{children}</div>;
}

export function CardTitle({ children }: { children: ReactNode }) {
  return <h3 className="mb-3 break-words text-sm font-semibold text-zinc-900 dark:text-zinc-100">{children}</h3>;
}

export function StatusBadge({ status }: { status: string }) {
  const map: Record<string, string> = {
    active: "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300",
    processed: "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300",
    posted: "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300",
    scheduled: "bg-sky-100 text-sky-700 dark:bg-sky-900/40 dark:text-sky-300",
    processing: "bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300",
    posting: "bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300",
    uploaded: "bg-zinc-200 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300",
    cooldown: "bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300",
    failed: "bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300",
    banned: "bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300",
    challenge_required: "bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300",
    disabled: "bg-zinc-200 text-zinc-500 dark:bg-zinc-800 dark:text-zinc-400",
    archived: "bg-zinc-200 text-zinc-500 dark:bg-zinc-800 dark:text-zinc-400",
  };
  const cls = map[status] ?? "bg-zinc-200 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300";
  const live = LIVE_STATUSES.has(status);
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-semibold ${cls}`}>
      {live && <span className="live-dot h-1.5 w-1.5 rounded-full bg-current" aria-hidden />}
      {status.replaceAll("_", " ")}
    </span>
  );
}

export function EmptyState({ title, hint, icon, action }: { title: string; hint?: string; icon?: ReactNode; action?: ReactNode }) {
  return (
    <div className="card flex flex-col items-center gap-1.5 p-6 text-center sm:p-10">
      <div className="mb-1 flex h-11 w-11 items-center justify-center rounded-full bg-zinc-100 text-zinc-400 dark:bg-zinc-800 dark:text-zinc-500">
        {icon ?? <Inbox className="h-5 w-5" />}
      </div>
      <p className="font-semibold">{title}</p>
      {hint && <p className="max-w-sm text-sm text-zinc-500">{hint}</p>}
      {action && <div className="mt-2">{action}</div>}
    </div>
  );
}

/** Query failure state with retry — use on every list page (isError branch). */
export function QueryFailed({ onRetry }: { onRetry: () => void }) {
  return (
    <div className="card flex flex-col items-center gap-2 p-6 text-center sm:p-10">
      <p className="font-semibold">Load failed</p>
      <p className="text-sm text-zinc-500">The request failed. Check the backend connection.</p>
      <button className="btn-ghost !px-3 !py-1 text-xs" onClick={onRetry}>Retry</button>
    </div>
  );
}

export function Spinner() {
  return (
    <div className="flex items-center justify-center p-10">
      <div className="h-8 w-8 animate-spin rounded-full border-2 border-zinc-300 border-t-emerald-600" />
    </div>
  );
}

/** Shimmering placeholder block — compose into skeleton screens. */
export function Skeleton({ className }: { className?: string }) {
  return <div className={cn("skeleton rounded-lg", className)} aria-hidden />;
}

/** Generic page skeleton: KPI-style cards + a wide content block. */
export function PageSkeleton() {
  return (
    <div className="space-y-6" aria-label="Loading">
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        {[0, 1, 2, 3].map((i) => (
          <Skeleton key={i} className="h-24" />
        ))}
      </div>
      <Skeleton className="h-72" />
    </div>
  );
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="block">
      <span className="label">{label}</span>
      {children}
    </label>
  );
}
