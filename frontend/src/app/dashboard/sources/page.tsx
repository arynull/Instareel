"use client";
import { useState } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { Card, CardTitle, EmptyState, Field, QueryFailed, Spinner } from "@/components/ui";
import { useApiMutation } from "@/hooks/use-api";
import { api } from "@/lib/api";
import type { SourceItem, VideoSource } from "@/types/models";

function useVideoSources(refetchInterval: number | false) {
  return useQuery({
    queryKey: ["video-sources"],
    queryFn: async () => (await api.get("/sources")).data as VideoSource[],
    refetchInterval,
  });
}

function StatusChip({ status }: { status: VideoSource["status"] }) {
  if (status === "running") {
    return (
      <span className="inline-flex shrink-0 items-center gap-1.5 rounded-full bg-emerald-100 px-2.5 py-0.5 text-xs font-semibold text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300">
        <span className="relative flex h-2 w-2">
          <span className="absolute h-full w-full animate-ping rounded-full bg-emerald-400 opacity-60" />
          <span className="h-2 w-2 rounded-full bg-emerald-500" />
        </span>
        running
      </span>
    );
  }
  const tone =
    status === "stopping"
      ? "bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300"
      : status === "failed"
        ? "bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300"
        : "bg-zinc-200 text-zinc-600 dark:bg-zinc-800 dark:text-zinc-300";
  return (
    <span className={`inline-flex shrink-0 items-center rounded-full px-2.5 py-0.5 text-xs font-semibold ${tone}`}>
      {status}
    </span>
  );
}

function ItemChip({ status }: { status: SourceItem["status"] }) {
  const tone =
    status === "downloaded"
      ? "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300"
      : status === "failed"
        ? "bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300"
        : status === "downloading"
          ? "bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300"
          : status === "skipped"
            ? "bg-zinc-200 text-zinc-500 dark:bg-zinc-800 dark:text-zinc-400"
            : "bg-sky-100 text-sky-700 dark:bg-sky-900/40 dark:text-sky-300";
  return (
    <span className={`inline-flex shrink-0 items-center rounded-full px-2 py-0.5 text-[11px] font-semibold ${tone}`}>
      {status}
    </span>
  );
}

function ItemsView({ sourceId }: { sourceId: number }) {
  const [filter, setFilter] = useState("");
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ["video-sources", sourceId, "items", filter],
    queryFn: async () =>
      (await api.get(`/sources/${sourceId}/items${filter ? `?status=${filter}` : ""}`)).data as SourceItem[],
  });
  const items = (data ?? []) as SourceItem[];
  return (
    <div className="mt-2 min-w-0 border-t border-zinc-100 pt-2 dark:border-zinc-800">
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <p className="text-xs font-bold">Items ({items.length})</p>
        <select
          className="input ml-auto !w-auto min-w-0 max-w-full !py-1 text-xs"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
        >
          <option value="">All statuses</option>
          <option value="pending">pending</option>
          <option value="downloading">downloading</option>
          <option value="downloaded">downloaded</option>
          <option value="skipped">skipped</option>
          <option value="failed">failed</option>
        </select>
      </div>
      {isLoading ? (
        <Spinner />
      ) : isError ? (
        <QueryFailed onRetry={() => refetch()} />
      ) : items.length === 0 ? (
        <p className="py-3 text-center text-xs text-zinc-400">No items yet — start the source to fetch.</p>
      ) : (
        <div className="mt-1 max-h-[300px] space-y-0 overflow-y-auto pr-1">
          {items.map((it) => (
            <div key={it.id} className="flex min-w-0 flex-wrap items-center gap-2 border-t border-zinc-100 py-1.5 text-xs first:border-0 dark:border-zinc-800">
              <code title={it.shortcode} className="min-w-0 max-w-full truncate">{it.shortcode}</code>
              <span className="shrink-0 text-zinc-400">{it.media_type}</span>
              <ItemChip status={it.status} />
              {it.video_id != null && it.status === "downloaded" ? (
                <Link href={`/dashboard/videos/${it.video_id}`} className="min-w-0 max-w-full truncate text-emerald-600 hover:underline">
                  #{it.video_id}
                </Link>
              ) : null}
              {it.error && <span title={it.error} className="min-w-0 max-w-full break-all text-red-400">· {it.error}</span>}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function EditForm({ src, onDone }: { src: VideoSource; onDone: () => void }) {
  const update = useApiMutation("put", [["video-sources"]], "Source updated");
  const [maxItems, setMaxItems] = useState(src.max_items);
  const [dMin, setDMin] = useState(src.delay_min_s);
  const [dMax, setDMax] = useState(src.delay_max_s);
  const [reelsOnly, setReelsOnly] = useState(src.reels_only);
  const [withCovers, setWithCovers] = useState(src.with_covers);
  const [autoProcess, setAutoProcess] = useState(src.auto_process);
  const delaysOk = dMin >= 0 && dMax >= 0 && dMin <= dMax;
  const maxOk = maxItems >= 1 && maxItems <= 200;

  async function save() {
    if (!delaysOk || !maxOk) return;
    await update.mutateAsync({
      url: `/sources/${src.id}`,
      body: {
        max_items: maxItems, delay_min_s: dMin, delay_max_s: dMax,
        reels_only: reelsOnly, with_covers: withCovers, auto_process: autoProcess,
      },
    });
    onDone();
  }

  return (
    <div className="mt-2 min-w-0 rounded-lg border border-zinc-100 p-2 dark:border-zinc-800">
      <div className="grid min-w-0 gap-2 sm:grid-cols-3">
        <Field label="Max items (1–200)">
          <input className="input max-w-full" type="number" min={1} max={200} value={maxItems} onChange={(e) => setMaxItems(Number(e.target.value))} />
        </Field>
        <Field label="Delay min (s)">
          <input className="input max-w-full" type="number" min={0} value={dMin} onChange={(e) => setDMin(Number(e.target.value))} />
        </Field>
        <Field label="Delay max (s)">
          <input className="input max-w-full" type="number" min={0} value={dMax} onChange={(e) => setDMax(Number(e.target.value))} />
        </Field>
      </div>
      <div className="mt-2 flex min-w-0 flex-wrap gap-x-4 gap-y-1 text-xs">
        <label className="flex items-center gap-1.5"><input type="checkbox" checked={reelsOnly} onChange={(e) => setReelsOnly(e.target.checked)} /> Reels only</label>
        <label className="flex items-center gap-1.5"><input type="checkbox" checked={withCovers} onChange={(e) => setWithCovers(e.target.checked)} /> With covers</label>
        <label className="flex items-center gap-1.5"><input type="checkbox" checked={autoProcess} onChange={(e) => setAutoProcess(e.target.checked)} /> Auto-process</label>
      </div>
      {(!delaysOk || !maxOk) && <p className="mt-1 text-xs text-red-500">Max must be 1–200 and delay min ≤ max.</p>}
      <div className="mt-2 flex flex-wrap gap-2">
        <button className="btn-primary !px-3 !py-1 text-xs" disabled={update.isPending || !delaysOk || !maxOk} onClick={save}>
          {update.isPending ? "Saving…" : "Save"}
        </button>
        <button className="btn-ghost !px-3 !py-1 text-xs" onClick={onDone}>Cancel</button>
      </div>
    </div>
  );
}

export default function SourcesPage() {
  const [expanded, setExpanded] = useState<number | null>(null);
  const [editing, setEditing] = useState<number | null>(null);
  const [form, setForm] = useState({
    username: "", max_items: 50,
    reels_only: true, with_covers: true, auto_process: true,
    delay_min_s: 8, delay_max_s: 20,
  });

  // Poll fast while anything is active (realtime WS invalidates this key
  // too — see use-realtime.ts video_source_update), slow otherwise.
  const { data: probe } = useVideoSources(false);
  const probeList = (probe ?? []) as VideoSource[];
  const anyBusy = probeList.some((s) => s.status === "running" || s.status === "stopping");
  const { data, isLoading, isError, refetch } = useVideoSources(anyBusy ? 3000 : 30000);

  const create = useApiMutation("post", [["video-sources"]], "Source added");
  const remove = useApiMutation("delete", [["video-sources"]], "Source deleted");
  const start = useApiMutation("post", [["video-sources"], ["videos"]]);
  const stop = useApiMutation("post", [["video-sources"]]);
  const retry = useApiMutation("post", [["video-sources"]]);

  const list = (data ?? probe ?? []) as VideoSource[];
  const usernameOk = form.username.trim().length > 0;
  const delaysOk = form.delay_min_s >= 0 && form.delay_max_s >= 0 && form.delay_min_s <= form.delay_max_s;
  const maxOk = form.max_items >= 1 && form.max_items <= 200;
  const canSubmit = usernameOk && delaysOk && maxOk && !create.isPending;

  function submit() {
    if (!canSubmit) return;
    create.mutate(
      {
        url: "/sources",
        body: {
          username: form.username.trim(),
          max_items: form.max_items,
          reels_only: form.reels_only, with_covers: form.with_covers, auto_process: form.auto_process,
          delay_min_s: form.delay_min_s, delay_max_s: form.delay_max_s,
        },
      },
      { onSuccess: () => setForm({ username: "", max_items: 50, reels_only: true, with_covers: true, auto_process: true, delay_min_s: 8, delay_max_s: 20 }) },
    );
  }

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-extrabold tracking-tight">Video sources</h1>

      <Card>
        <CardTitle>New source</CardTitle>
        <div className="grid min-w-0 gap-3 md:grid-cols-4">
          <Field label="Username">
            <input
              className="input max-w-full"
              value={form.username}
              onChange={(e) => setForm({ ...form, username: e.target.value })}
              placeholder="@username"
            />
          </Field>
          <Field label="Max items (1–200)">
            <input className="input max-w-full" type="number" min={1} max={200} value={form.max_items} onChange={(e) => setForm({ ...form, max_items: Number(e.target.value) })} />
          </Field>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-2">
            <Field label="Delay min (s)">
              <input className="input max-w-full" type="number" min={0} value={form.delay_min_s} onChange={(e) => setForm({ ...form, delay_min_s: Number(e.target.value) })} />
            </Field>
            <Field label="Delay max (s)">
              <input className="input max-w-full" type="number" min={0} value={form.delay_max_s} onChange={(e) => setForm({ ...form, delay_max_s: Number(e.target.value) })} />
            </Field>
          </div>
        </div>
        <div className="mt-3 flex min-w-0 flex-wrap items-center gap-x-4 gap-y-1 text-sm">
          <label className="flex items-center gap-1.5"><input type="checkbox" checked={form.reels_only} onChange={(e) => setForm({ ...form, reels_only: e.target.checked })} /> Reels only</label>
          <label className="flex items-center gap-1.5"><input type="checkbox" checked={form.with_covers} onChange={(e) => setForm({ ...form, with_covers: e.target.checked })} /> With covers</label>
          <label className="flex items-center gap-1.5"><input type="checkbox" checked={form.auto_process} onChange={(e) => setForm({ ...form, auto_process: e.target.checked })} /> Auto-process</label>
          <button className="btn-primary ml-auto w-full sm:w-auto" disabled={!canSubmit} onClick={submit}>
            {create.isPending ? "Adding…" : "Add source"}
          </button>
        </div>
        {(!usernameOk || !delaysOk || !maxOk) && (
          <p className="mt-2 text-xs text-zinc-500">
            {!usernameOk ? "Username is required. " : ""}{!maxOk ? "Max must be 1–200. " : ""}{!delaysOk ? "Delay min must be ≤ max." : ""}
          </p>
        )}
      </Card>

      {isLoading ? <Spinner /> : isError ? <QueryFailed onRetry={() => refetch()} /> : list.length === 0 ? (
        <EmptyState title="No video sources" hint="Add an Instagram username above to start fetching reels." />
      ) : (
        <div className="space-y-3">
          {list.map((s) => {
            const busy = s.status === "running" || s.status === "stopping";
            return (
              <Card key={s.id}>
                <div className="flex min-w-0 flex-wrap items-center gap-2">
                  <strong title={s.username} className="min-w-0 max-w-full truncate">@{s.username}</strong>
                  <StatusChip status={s.status} />
                  <span className="ml-auto flex shrink-0 flex-wrap gap-2">
                    {s.status === "running" ? (
                      <button className="btn-ghost !px-3 !py-1 text-xs" disabled={stop.isPending} onClick={() => stop.mutate({ url: `/sources/${s.id}/stop` })}>
                        {stop.isPending ? "Stopping…" : "Stop"}
                      </button>
                    ) : (
                      <button className="btn-ghost !px-3 !py-1 text-xs" disabled={start.isPending} onClick={() => start.mutate({ url: `/sources/${s.id}/start` })}>
                        {start.isPending ? "Starting…" : "Start"}
                      </button>
                    )}
                    {s.failed_count > 0 && (
                      <button className="btn-ghost !px-3 !py-1 text-xs" disabled={retry.isPending} onClick={() => retry.mutate({ url: `/sources/${s.id}/retry-failed` })}>
                        {retry.isPending ? "Retrying…" : `Retry failed (${s.failed_count})`}
                      </button>
                    )}
                    {!busy && (
                      <button className="btn-ghost !px-3 !py-1 text-xs" onClick={() => setEditing(editing === s.id ? null : s.id)}>
                        Settings
                      </button>
                    )}
                    <button
                      className="btn-ghost !px-3 !py-1 text-xs"
                      onClick={() => setExpanded(expanded === s.id ? null : s.id)}
                    >
                      {expanded === s.id ? "Hide items" : "Items"}
                    </button>
                    {!busy && (
                      <button
                        className="btn-ghost !px-3 !py-1 text-xs !text-red-500"
                        disabled={remove.isPending}
                        onClick={() => {
                          if (confirm(`Delete source @${s.username}?\n\nDownloaded videos stay in the library. This cannot be undone.`))
                            remove.mutate({ url: `/sources/${s.id}` });
                        }}
                      >
                        {remove.isPending ? "Deleting…" : "Delete"}
                      </button>
                    )}
                  </span>
                </div>
                <p className="mt-1 min-w-0 break-words text-xs text-zinc-500">
                  downloaded {s.downloaded}/{s.max_items} · fetched {s.fetched} · skipped {s.skipped} · failed {s.failed_count}
                  {s.pending_items > 0 ? ` · pending ${s.pending_items}` : ""} · max {s.max_items} · delay {s.delay_min_s}–{s.delay_max_s}s
                </p>
                {s.current_stage && <p title={s.current_stage} className="mt-0.5 min-w-0 max-w-full truncate text-xs text-zinc-500">{s.current_stage}</p>}
                {s.last_error && <p title={s.last_error} className="mt-0.5 min-w-0 max-w-full break-all text-xs text-red-500">{s.last_error}</p>}
                {editing === s.id && !busy && <EditForm src={s} onDone={() => setEditing(null)} />}
                {expanded === s.id && <ItemsView sourceId={s.id} />}
              </Card>
            );
          })}
        </div>
      )}
    </div>
  );
}
