"use client";
import type { Account, LogEntry, Post } from "@/types/models";
import { fmt, timeAgo } from "@/lib/utils";
import { comparePostRecency } from "./sort";

export function PhoneActivity({
  account,
  posts,
  logs,
}: {
  account: Account;
  posts: Post[];
  logs: LogEntry[];
}) {
  const mine = posts
    .filter((p) => p.account_id === account.id && p.status === "posted")
    .sort(comparePostRecency)
    .slice(0, 5);
  const recent = logs.slice(0, 20);

  return (
    <div className="flex h-full flex-col gap-3 overflow-y-auto p-3 text-sm">
      <div>
        <p className="mb-1 font-bold">Top reels</p>
        {mine.length === 0 && <p className="text-xs text-zinc-500">Nothing posted yet.</p>}
        {mine.map((p) => (
          <div key={p.id} className="flex items-center gap-2 border-b border-zinc-100 py-1.5 text-xs dark:border-zinc-800">
            <span className="font-semibold">#{p.id}</span>
            <span className="truncate">{[p.caption, p.hashtags].filter(Boolean).join(" ") || "—"}</span>
            <span className="ml-auto shrink-0 text-zinc-500">
              {fmt(p.views_7d ?? p.views_24h)} views · {p.engagement_rate ?? 0}%
            </span>
          </div>
        ))}
      </div>
      <div>
        <p className="mb-1 font-bold">System activity</p>
        {recent.length === 0 && <p className="text-xs text-zinc-500">No events yet.</p>}
        {recent.map((l) => (
          <div key={l.id} className="border-b border-zinc-100 py-1.5 text-xs dark:border-zinc-800">
            <p className="truncate">{l.message}</p>
            <p className="text-zinc-500">
              {l.category} · {timeAgo(l.timestamp)}
            </p>
          </div>
        ))}
      </div>
    </div>
  );
}
