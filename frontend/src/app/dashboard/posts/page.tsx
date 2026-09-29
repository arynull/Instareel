"use client";
import { useState } from "react";
import { Card, EmptyState, QueryFailed, Spinner, StatusBadge } from "@/components/ui";
import { WhyPopover } from "@/components/why-popover";
import { useApiMutation, usePosts, useQueue } from "@/hooks/use-api";
import { fmt, parseApiDate, timeAgo } from "@/lib/utils";
import type { Post } from "@/types/models";

export default function PostsPage() {
  const [status, setStatus] = useState("");
  const { data, isLoading, isError, refetch } = usePosts(status);
  const { data: queue } = useQueue();
  const retry = useApiMutation("post", [["posts"]]);
  const remove = useApiMutation("delete", [["posts"]], "Post deleted");
  const [busyId, setBusyId] = useState<number | null>(null);
  const posts = (data ?? []) as Post[];

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="text-xl font-extrabold tracking-tight">Posts</h1>
        <select className="input ml-auto !w-auto" value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="">All statuses</option>
          {["scheduled", "posting", "posted", "failed"].map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
      </div>

      <Card>
        <p className="mb-2 text-sm font-semibold">Up next ({((queue ?? []) as Post[]).length})</p>
        {((queue ?? []) as Post[]).slice(0, 5).map((p) => (
          <div key={p.id} className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5 border-t border-zinc-100 py-1.5 text-sm first:border-0 dark:border-zinc-800">
            <span className="shrink-0">Post #{p.id}</span>
            <span className="min-w-0 flex-1 truncate text-zinc-500">account #{p.account_id} · video #{p.video_id}</span>
            <span className="shrink-0 whitespace-nowrap text-zinc-500">{p.scheduled_for ? timeAgo(p.scheduled_for) : "asap"}</span>
          </div>
        ))}
        {(queue ?? []).length === 0 && <p className="text-sm text-zinc-500">Nothing scheduled.</p>}
      </Card>

      {isLoading ? <Spinner /> : isError ? <QueryFailed onRetry={() => refetch()} /> : posts.length === 0 ? (
        <EmptyState title="No posts" hint="Schedule rules create posts automatically every minute." />
      ) : (
        <Card>
          <div className="-mx-4 overflow-x-auto px-4 sm:mx-0 sm:px-0">
            <table className="w-full min-w-[720px] text-sm">
              <thead>
                  <tr className="text-left text-xs uppercase text-zinc-400">
                    <th className="py-2 pr-4">Post</th><th className="py-2 pr-4">Status</th>
                    <th className="py-2 pr-4">Audio</th>
                    <th className="py-2 pr-4 text-right">Views</th><th className="py-2 pr-4 text-right">Eng.</th>
                    <th className="py-2 pr-4 text-right">Checked</th>
                    <th className="py-2 pr-4">Link</th><th className="py-2 text-right">Actions</th>
                  </tr>
              </thead>
              <tbody>
                {posts.map((p) => (
                  <tr key={p.id} className="border-t border-zinc-100 dark:border-zinc-800">
                    <td className="py-2 pr-4">#{p.id} · acc #{p.account_id} · vid #{p.video_id}</td>
                    <td className="py-2 pr-4">
                      <span className="inline-flex items-center gap-1">
                        <StatusBadge status={p.status} />
                        {p.status === "failed" && p.fail_reason && (
                          <WhyPopover label={`Why did post #${p.id} fail?`}>
                            <p className="mb-1 font-semibold">Why it failed</p>
                            <p className="break-words text-zinc-600 dark:text-zinc-300">{p.fail_reason}</p>
                            {p.retry_count > 0 && (
                              <p className="mt-1.5 text-zinc-400">Retried {p.retry_count}× — fix the cause, then hit Retry.</p>
                            )}
                          </WhyPopover>
                        )}
                      </span>
                    </td>
                    <td className="max-w-[160px] truncate py-2 pr-4 text-zinc-500" title={p.audio_track ?? ""}>{p.audio_track ?? "—"}{p.is_trial ? " · trial" : ""}</td>
                    <td className="py-2 pr-4 text-right">{fmt(p.views_7d ?? p.views_24h)}</td>
                    <td className="py-2 pr-4 text-right">{p.engagement_rate != null ? `${p.engagement_rate}%` : "—"}</td>
                    <td
                      className="whitespace-nowrap py-2 pr-4 text-right text-xs text-zinc-500"
                      title={p.last_analytics_check ? parseApiDate(p.last_analytics_check).toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : undefined}
                    >
                      {p.last_analytics_check ? timeAgo(p.last_analytics_check) : "—"}
                    </td>
                    <td className="py-2 pr-4">{p.ig_permalink ? <a className="text-emerald-500 hover:underline" href={p.ig_permalink} target="_blank">Reel ↗</a> : "—"}</td>
                    <td className="whitespace-nowrap py-2 text-right">
                      {p.status === "failed" && <button className="btn-ghost mr-2 !px-3 !py-1 text-xs" disabled={busyId === p.id} onClick={async () => { setBusyId(p.id); try { await retry.mutateAsync({ url: `/posts/${p.id}/retry` }); } finally { setBusyId(null); } }}>{busyId === p.id ? "Retrying…" : "Retry"}</button>}
                      <button className="btn-ghost !px-3 !py-1 text-xs text-red-500" disabled={busyId === p.id} onClick={async () => { if (!confirm(`Delete post #${p.id}?`)) return; setBusyId(p.id); try { await remove.mutateAsync({ url: `/posts/${p.id}` }); } finally { setBusyId(null); } }}>{busyId === p.id ? "Deleting…" : "Delete"}</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </div>
  );
}
