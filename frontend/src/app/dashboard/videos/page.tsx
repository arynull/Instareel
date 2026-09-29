"use client";
import Link from "next/link";
import { useState } from "react";
import { Plus } from "lucide-react";
import { Card, EmptyState, QueryFailed, Spinner, StatusBadge } from "@/components/ui";
import { VideosArt } from "@/components/empty-art";
import { useApiMutation, useVideos } from "@/hooks/use-api";
import { timeAgo } from "@/lib/utils";
import type { Video } from "@/types/models";

export default function VideosPage() {
  const [status, setStatus] = useState("");
  const { data, isLoading, isError, refetch } = useVideos(status);
  const del = useApiMutation("delete", [[ "videos" ]]);
  const process = useApiMutation("post", [["videos"]]);
  const [procBusyId, setProcBusyId] = useState<number | null>(null);
  const [delBusyId, setDelBusyId] = useState<number | null>(null);
  const videos = (data ?? []) as Video[];

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="text-xl font-extrabold tracking-tight">Videos</h1>
        <div className="ml-auto flex flex-wrap justify-end gap-2">
          <select className="input !w-auto" value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="">All statuses</option>
            {["uploaded", "processing", "processed", "posting", "posted", "failed", "archived"].map((s) => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>
          <Link href="/dashboard/videos/upload" className="btn-primary"><Plus className="h-4 w-4" /> Upload</Link>
        </div>
      </div>
      {isLoading ? <Spinner /> : isError ? <QueryFailed onRetry={() => refetch()} /> : videos.length === 0 ? (
        <EmptyState title="No videos" hint="Upload your first video to start the funnel." icon={<VideosArt className="h-12 w-12 text-zinc-300 dark:text-zinc-600" />} />
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {videos.map((v) => (
            <Card key={v.id}>
              <div className="mb-2 flex items-center justify-between gap-2">
                <Link href={`/dashboard/videos/${v.id}`} title={v.original_filename} className="min-w-0 flex-1 truncate font-semibold hover:text-emerald-500">
                  #{v.id} · {v.original_filename}
                </Link>
                <span className="shrink-0"><StatusBadge status={v.status} /></span>
              </div>
              <p className="text-xs text-zinc-500">
                {v.duration ? `${v.duration.toFixed(1)}s` : "—"} · {timeAgo(v.created_at)}
              </p>
              {v.failed_reason && (
                <p title={v.failed_reason} className="mt-1 truncate rounded-md bg-red-50 px-2 py-1 text-xs text-red-600 dark:bg-red-950 dark:text-red-400">
                  ⚠ {v.failed_reason}
                </p>
              )}
              <div className="mt-3 flex flex-wrap gap-2">
                <Link href={`/dashboard/videos/${v.id}`} className="btn-ghost min-w-0 flex-1 !py-1.5 text-xs">Detail</Link>
                {(v.status === "uploaded" || v.status === "failed") && (
                  <button
                    className="btn-primary min-w-0 flex-1 !py-1.5 text-xs"
                    disabled={procBusyId === v.id}
                    onClick={async () => { setProcBusyId(v.id); try { await process.mutateAsync({ url: `/videos/${v.id}/process` }); } finally { setProcBusyId(null); } }}
                  >
                    {procBusyId === v.id ? "Processing…" : "Process"}
                  </button>
                )}
                <button
                  className="btn-ghost !py-1.5 text-xs text-red-500"
                  disabled={delBusyId === v.id}
                  onClick={async () => { if (!confirm(`Delete video #${v.id}?`)) return; setDelBusyId(v.id); try { await del.mutateAsync({ url: `/videos/${v.id}` }); } finally { setDelBusyId(null); } }}
                >
                  {delBusyId === v.id ? "Deleting…" : "Delete"}
                </button>
              </div>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
