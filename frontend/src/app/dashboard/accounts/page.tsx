"use client";
import { useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { api } from "@/lib/api";
import { Card, EmptyState, Field, QueryFailed, Spinner, StatusBadge } from "@/components/ui";
import { toast } from "@/components/toast";
import { useAccounts, useApiMutation, useProxies } from "@/hooks/use-api";
import { proxyHost, timeAgo } from "@/lib/utils";
import type { Account, Proxy } from "@/types/models";

export default function AccountsPage() {
  const { data, isLoading, isError, refetch } = useAccounts();
  const qc = useQueryClient();
  const { data: proxies } = useProxies();
  const create = useApiMutation("post", [["accounts"]], "Account added");
  const remove = useApiMutation("delete", [["accounts"]], "Account removed");
  const action = useApiMutation("post", [["accounts"]]);
  const update = useApiMutation("put", [["accounts"]]);
  const [form, setForm] = useState({ username: "", password: "", max_daily_posts: 3 });
  const [uploadingId, setUploadingId] = useState<number | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const [uploadTarget, setUploadTarget] = useState<number | null>(null);
  const accounts = (data ?? []) as Account[];
  const proxyList = (proxies ?? []) as Proxy[];

  function proxyLabel(p: Proxy): string {
    // <option> can't wrap — keep host short; full URL is on the Proxies page.
    let host = proxyHost(p.url);
    if (host.length > 26) host = `${host.slice(0, 25)}…`;
    return `${p.protocol}://${host}${p.country ? ` (${p.country})` : ""}${p.is_healthy ? "" : " [down]"}`;
  }

  async function uploadSession(accountId: number, file: File) {
    setUploadingId(accountId);
    try {
      const formData = new FormData();
      formData.append("file", file);
      const { data: res } = await api.post(`/accounts/${accountId}/session`, formData, {
        headers: { "Content-Type": "multipart/form-data" },
        timeout: 60000,
      });
      toast("success", String(res.detail ?? "Session uploaded"));
      qc.invalidateQueries({ queryKey: ["accounts"] });
    } catch (e: unknown) {
      const msg = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Session upload failed";
      toast("error", String(msg));
    } finally {
      setUploadingId(null);
      setUploadTarget(null);
      if (fileRef.current) fileRef.current.value = "";
    }
  }

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-extrabold tracking-tight">Instagram accounts</h1>
      <input
        ref={fileRef}
        type="file"
        accept=".json,application/json"
        className="hidden"
        onChange={(e) => {
          const f = e.target.files?.[0];
          if (f && uploadTarget != null) uploadSession(uploadTarget, f);
        }}
      />
      <Card>
        <div className="grid gap-3 md:grid-cols-4">
          <Field label="Username"><input className="input" value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} placeholder="instagram_user" /></Field>
          <Field label="Password"><input className="input" type="password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} /></Field>
          <Field label="Max posts/day"><input className="input" type="number" min={1} max={20} value={form.max_daily_posts} onChange={(e) => setForm({ ...form, max_daily_posts: Number(e.target.value) })} /></Field>
          <div className="flex items-end">
            <button
              className="btn-primary w-full" disabled={!form.username || !form.password || create.isPending}
              onClick={() => create.mutate({ url: "/accounts", body: form }, { onSuccess: () => setForm({ username: "", password: "", max_daily_posts: 3 }) })}
            >
              {create.isPending ? "Adding…" : "Add account"}
            </button>
          </div>
        </div>
      </Card>
      {isLoading ? <Spinner /> : isError ? <QueryFailed onRetry={() => refetch()} /> : accounts.length === 0 ? (
        <EmptyState title="No accounts" hint="Add your first Instagram account above." />
      ) : (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {accounts.map((a) => {
            const busyUrl = (action.variables as { url?: string } | undefined)?.url;
            const busy = (u: string) => action.isPending && busyUrl === u;
            const actBtn = (u: string, label: string) => (
              <button
                key={u}
                className="btn-ghost !px-3 !py-1.5 text-xs"
                disabled={action.isPending}
                onClick={() => action.mutate({ url: u })}
              >
                {busy(u) ? "Working…" : label}
              </button>
            );
            return (
            <Card key={a.id}>
              <div className="flex min-w-0 flex-wrap items-center gap-2">
                <Link href={`/dashboard/accounts/${a.id}`} title={a.username} className="min-w-0 flex-1 truncate break-all font-semibold hover:text-emerald-500">@{a.username}</Link>
                <span className="ml-auto shrink-0"><StatusBadge status={a.status} /></span>
              </div>
              <p className="mt-1 text-xs text-zinc-500">
                {a.posts_today}/{a.max_daily_posts} today · {a.total_posts} total · {a.total_views} views · last post {timeAgo(a.last_post)}
              </p>
              <p className="mt-1 text-xs text-zinc-500">
                Session: {a.has_session
                  ? <span className="font-semibold text-emerald-500">saved ✓</span>
                  : <span className="font-semibold text-amber-500">missing</span>}
                {" · "}Proxy: {a.proxy_id ? ` #${a.proxy_id}` : " none"}
              </p>
              <div className="mt-3">
                <Field label="Proxy">
                  <select
                    className="input !py-1.5 text-xs"
                    value={a.proxy_id ? String(a.proxy_id) : ""}
                    disabled={update.isPending}
                    onChange={(e) => {
                      const v = e.target.value;
                      update.mutate({ url: `/accounts/${a.id}`, body: { proxy_id: v === "" ? "none" : Number(v) } });
                    }}
                  >
                    <option value="">No proxy</option>
                    {proxyList.map((p) => (
                      <option key={p.id} value={p.id}>{proxyLabel(p)}</option>
                    ))}
                  </select>
                </Field>
              </div>
              <div className="mt-3 flex flex-wrap gap-2">
                {actBtn(`/accounts/${a.id}/login`, "Login")}
                {actBtn(`/accounts/${a.id}/test-session`, "Test session")}
                <button
                  className="btn-ghost !px-3 !py-1.5 text-xs"
                  disabled={action.isPending}
                  title="Username changed on Instagram? Rename the row — history, stats and session follow it."
                  onClick={() => {
                    const next = prompt(`Rename @${a.username} to:`, a.username)?.trim().replace(/^@/, "");
                    if (next) action.mutate({ url: `/accounts/${a.id}/rename?new_username=${encodeURIComponent(next)}` });
                  }}
                >
                  Rename
                </button>
                <button
                  className="btn-ghost !px-3 !py-1.5 text-xs"
                  disabled={uploadingId === a.id}
                  onClick={() => { setUploadTarget(a.id); fileRef.current?.click(); }}
                >
                  {uploadingId === a.id ? "Uploading…" : "Upload session"}
                </button>
                {a.status === "active"
                  ? actBtn(`/accounts/${a.id}/cooldown`, "Cooldown")
                  : actBtn(`/accounts/${a.id}/activate`, "Activate")}
                <button
                  className="btn-ghost !px-3 !py-1.5 text-xs text-red-500"
                  disabled={remove.isPending}
                  onClick={() => { if (confirm(`Remove @${a.username}?`)) remove.mutate({ url: `/accounts/${a.id}` }); }}
                >
                  {remove.isPending ? "Removing…" : "Remove"}
                </button>
              </div>
            </Card>
            );
          })}
        </div>
      )}
    </div>
  );
}
