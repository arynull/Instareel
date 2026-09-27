"use client";
import { useState } from "react";
import { Card, CardTitle, EmptyState, Field, QueryFailed, Spinner } from "@/components/ui";
import { useApiMutation, useCaptions, useHashtags } from "@/hooks/use-api";
import type { Caption, HashtagSet } from "@/types/models";

export default function CaptionsPage() {
  const [tab, setTab] = useState<"captions" | "hashtags">("captions");
  const { data: caps, isLoading: l1, isError: e1, refetch: r1 } = useCaptions();
  const { data: tags, isLoading: l2, isError: e2, refetch: r2 } = useHashtags();
  const createCap = useApiMutation("post", [["captions"]], "Caption added");
  const delCap = useApiMutation("delete", [["captions"]], "Caption deleted");
  const createTag = useApiMutation("post", [["hashtags"]], "Hashtag set added");
  const delTag = useApiMutation("delete", [["hashtags"]], "Hashtag set deleted");
  const [cap, setCap] = useState({ name: "", content: "", category: "" });
  const [tag, setTag] = useState({ name: "", tags: "" });

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-extrabold tracking-tight">Captions & hashtags</h1>
      <div className="flex gap-2">
        {(["captions", "hashtags"] as const).map((t) => (
          <button key={t} onClick={() => setTab(t)} className={`rounded-lg px-4 py-2 text-sm font-semibold ${tab === t ? "bg-emerald-600 text-white" : "btn-ghost"}`}>
            {t === "captions" ? "Captions" : "Hashtags"}
          </button>
        ))}
      </div>

      {tab === "captions" ? (
        <>
          <Card>
            <CardTitle>New caption template</CardTitle>
            <div className="grid gap-3 md:grid-cols-3">
              <Field label="Name"><input className="input" value={cap.name} onChange={(e) => setCap({ ...cap, name: e.target.value })} /></Field>
              <Field label="Category"><input className="input" value={cap.category} onChange={(e) => setCap({ ...cap, category: e.target.value })} placeholder="optional" /></Field>
              <div className="flex items-end"><button className="btn-primary w-full" disabled={!cap.name || !cap.content || createCap.isPending} onClick={() => createCap.mutate({ url: "/captions", body: { ...cap, category: cap.category || null } }, { onSuccess: () => setCap({ name: "", content: "", category: "" }) })}>{createCap.isPending ? "Adding…" : "Add"}</button></div>
            </div>
            <div className="mt-3"><Field label="Content (emoji + line breaks supported)"><textarea className="input" rows={3} value={cap.content} onChange={(e) => setCap({ ...cap, content: e.target.value })} /></Field></div>
          </Card>
          {l1 ? <Spinner /> : e1 ? <QueryFailed onRetry={() => r1()} /> : ((caps ?? []) as Caption[]).length === 0 ? <EmptyState title="No captions" /> : (
            <div className="grid gap-4 md:grid-cols-2">
              {((caps ?? []) as Caption[]).map((c) => (
                <Card key={c.id}>
                  <div className="flex min-w-0 flex-wrap items-center gap-2">
                    <strong title={c.name} className="min-w-0 flex-1 truncate">{c.name}</strong>
                    {c.category && <span title={c.category} className="max-w-full shrink-0 truncate rounded-full bg-zinc-100 px-2 py-0.5 text-xs dark:bg-zinc-800">{c.category}</span>}
                    <span className="shrink-0 text-xs text-zinc-500">used {c.use_count}× · {c.avg_engagement ?? 0}% eng.</span>
                  </div>
                  <div className="mt-2 break-words rounded-lg bg-zinc-50 p-3 text-sm whitespace-pre-wrap dark:bg-zinc-800/60">{c.content}</div>
                  <div className="mt-2 min-w-0 rounded-lg border border-zinc-200 p-3 text-xs text-zinc-500 dark:border-zinc-800">
                    <p className="font-semibold text-zinc-700 dark:text-zinc-300">Instagram preview</p>
                    <p className="break-words whitespace-pre-wrap"><strong>yourpage</strong> {c.content.slice(0, 140)}{c.content.length > 140 ? "…" : ""}</p>
                  </div>
                  <button className="btn-ghost mt-2 !py-1 text-xs text-red-500" disabled={delCap.isPending} onClick={() => { if (confirm(`Delete "${c.name}"?`)) delCap.mutate({ url: `/captions/${c.id}` }); }}>{delCap.isPending ? "Deleting…" : "Delete"}</button>
                </Card>
              ))}
            </div>
          )}
        </>
      ) : (
        <>
          <Card>
            <CardTitle>New hashtag set</CardTitle>
            <div className="grid gap-3 md:grid-cols-3">
              <Field label="Name"><input className="input" value={tag.name} onChange={(e) => setTag({ ...tag, name: e.target.value })} /></Field>
              <Field label="Tags (comma separated)">
                <input className="input" value={tag.tags} onChange={(e) => setTag({ ...tag, tags: e.target.value })} placeholder="#reels, #viral, …" />
              </Field>
              <div className="flex items-end"><button className="btn-primary w-full" disabled={!tag.name || !tag.tags || createTag.isPending} onClick={() => createTag.mutate({ url: "/hashtags", body: tag }, { onSuccess: () => setTag({ name: "", tags: "" }) })}>{createTag.isPending ? "Adding…" : "Add"}</button></div>
            </div>
          </Card>
          {l2 ? <Spinner /> : e2 ? <QueryFailed onRetry={() => r2()} /> : ((tags ?? []) as HashtagSet[]).length === 0 ? <EmptyState title="No hashtag sets" /> : (
            <div className="grid gap-4 md:grid-cols-2">
              {((tags ?? []) as HashtagSet[]).map((h) => (
                <Card key={h.id}>
                  <div className="flex min-w-0 flex-wrap items-center gap-2"><strong title={h.name} className="min-w-0 flex-1 truncate">{h.name}</strong><span className="shrink-0 text-xs text-zinc-500">used {h.use_count}×</span></div>
                  <p className="mt-2 break-all text-sm text-sky-600 dark:text-sky-400">{h.tags}</p>
                  <button className="btn-ghost mt-2 !py-1 text-xs text-red-500" disabled={delTag.isPending} onClick={() => { if (confirm(`Delete "${h.name}"?`)) delTag.mutate({ url: `/hashtags/${h.id}` }); }}>{delTag.isPending ? "Deleting…" : "Delete"}</button>
                </Card>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}
