"use client";
import { timeAgo } from "@/lib/utils";
import { useNow } from "@/hooks/use-now";

/** Relative timestamp that refreshes itself every 30s (no page reload needed). */
export function LiveTimeAgo({
  iso,
  className,
  title,
}: {
  iso: string | null | undefined;
  className?: string;
  title?: string;
}) {
  useNow();
  if (!iso) return <span className={className}>never</span>;
  return (
    <span className={className} title={title}>
      {timeAgo(iso)}
    </span>
  );
}
