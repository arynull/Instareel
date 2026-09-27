"use client";
import { useState } from "react";
import { Card, CardTitle, EmptyState, Field, QueryFailed, Spinner } from "@/components/ui";
import { useApiMutation, useEffects } from "@/hooks/use-api";
import type { Effect } from "@/types/models";

export default function EffectsPage() {
  const { data, isLoading, isError, refetch } = useEffects();
  const create = useApiMutation("post", [["effects"]], "Preset added");
  const remove = useApiMutation("delete", [["effects"]], "Preset deleted");
  const [form, setForm] = useState({ name: "", description: "", ffmpeg_filter: "" });
  const effects = (data ?? []) as Effect[];

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-extrabold tracking-tight">Video effect presets</h1>
      <Card>
        <CardTitle>New preset</CardTitle>
        <div className="grid gap-3 md:grid-cols-3">
          <Field label="Name"><input className="input" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="warm_boost" /></Field>
          <Field label="Description"><input className="input" value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} placeholder="Warm color grade + slight sharpen" /></Field>
          <div className="flex items-end"><button className="btn-primary w-full" disabled={!form.name || create.isPending} onClick={() => create.mutate({ url: "/effects", body: form }, { onSuccess: () => setForm({ name: "", description: "", ffmpeg_filter: "" }) })}>{create.isPending ? "Adding…" : "Add"}</button></div>
        </div>
        <div className="mt-3"><Field label="FFmpeg video filter (applied after crop/scale)"><input className="input font-mono text-xs" value={form.ffmpeg_filter} onChange={(e) => setForm({ ...form, ffmpeg_filter: e.target.value })} placeholder="eq=saturation=1.2:contrast=1.05,unsharp=5:5:0.5" /></Field></div>
      </Card>
      {isLoading ? <Spinner /> : isError ? <QueryFailed onRetry={() => refetch()} /> : effects.length === 0 ? <EmptyState title="No presets" hint="Reload to seed the built-in professional presets, or add your own FFmpeg filters." /> : (
        <div className="grid gap-4 md:grid-cols-2">
          {effects.map((e) => (
            <Card key={e.id}>
              <div className="flex min-w-0 flex-wrap items-center gap-2"><strong title={e.name} className="min-w-0 flex-1 truncate">{e.name}</strong><span className="shrink-0 text-xs text-zinc-500">used {e.use_count}× · {e.avg_engagement ?? 0}% eng.</span></div>
              <p className="mt-1 break-words text-sm text-zinc-500">{e.description}</p>
              <code className="mt-2 block max-w-full overflow-x-auto whitespace-pre-wrap break-all rounded bg-zinc-100 p-2 text-xs dark:bg-zinc-800">{e.ffmpeg_filter || "(no filter — plain crop/scale/encode)"}</code>
              <button className="btn-ghost mt-2 !py-1 text-xs text-red-500" disabled={remove.isPending} onClick={() => { if (confirm(`Delete "${e.name}"?`)) remove.mutate({ url: `/effects/${e.id}` }); }}>{remove.isPending ? "Deleting…" : "Delete"}</button>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
