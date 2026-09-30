"use client";
import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Card, CardTitle, EmptyState, Field, QueryFailed, Spinner, StatusBadge } from "@/components/ui";
import { useApiMutation, useProxies, useProxySources } from "@/hooks/use-api";
import { api } from "@/lib/api";
import { proxyHost, timeAgo } from "@/lib/utils";
import type { Proxy, ProxyImportResult, ProxySource } from "@/types/models";

interface PipelineRun { at: string; message: string }
interface Pipeline {
  counts: { total: number; healthy: number; dead: number; disabled: number; never_checked: number; manual: number; auto: number };
  latency: { avg_ms: number | null; max_ms: number | null; measured: number };
  oldest_checked_at: string | null;
  checker: { cadence: string; batch: number; threads: number; verify_limit: number; verify_threads: number; sweep_timeout_s: number; max_fails: number; fail_cooldown_h: number };
  pool: { refresh_cadence: string; purge_after_days: number; stillborn_hours: number; max_auto: number; country: string; require_country: boolean };
  last_runs: { health_check: PipelineRun | null; pool_refresh: PipelineRun | null; purge: PipelineRun | null; auto_disabled: PipelineRun | null };
  recent: { at: string; level: string; message: string }[];
}

function RunLine({ label, run }: { label: string; run: PipelineRun | null }) {
  return (
    <p className="text-xs text-zinc-500">
      <span className="font-semibold text-zinc-700 dark:text-zinc-300">{label}: </span>
      {run ? <><span title={run.message} className="break-words">{run.message.slice(0, 90)}</span> <span className="text-zinc-400">· {timeAgo(run.at)}</span></> : "never yet"}
    </p>
  );
}

/** Granular view of the proxy pipeline: pool snapshot, checker config +
 *  last cycle, pool policy + last refresh, and recent proxy activity.
 *  Polls every 20s so a running check-all visibly lands without reload. */
function PipelineStatus() {
  const { data } = useQuery({
    queryKey: ["proxy-pipeline"],
    queryFn: async () => (await api.get("/proxies/pipeline")).data as Pipeline,
    // Realtime first (WS proxy_pool_update invalidates this key); the poll
    // below is only a backstop for dropped frames.
    refetchInterval: 300000,
  });
  if (!data) return null;
  const c = data.counts;
  const tiles: { label: string; value: string; tone: string }[] = [
    { label: "Healthy", value: `${c.healthy}/${c.total}`, tone: "text-emerald-500" },
    { label: "Dead", value: String(c.dead), tone: c.dead ? "text-red-500" : "text-zinc-500" },
    { label: "Disabled", value: String(c.disabled), tone: "text-zinc-500" },
    { label: "Never checked", value: String(c.never_checked), tone: c.never_checked ? "text-amber-500" : "text-zinc-500" },
    { label: "Avg latency", value: data.latency.avg_ms != null ? `${data.latency.avg_ms}ms` : "—", tone: "text-zinc-700 dark:text-zinc-300" },
    { label: "Oldest check", value: data.oldest_checked_at ? timeAgo(data.oldest_checked_at) : "—", tone: "text-zinc-700 dark:text-zinc-300" },
  ];
  return (
    <Card>
      <div className="mb-2 flex items-center gap-2">
        <CardTitle>Pipeline status</CardTitle>
        <span className="relative flex h-2 w-2"><span className="absolute h-full w-full animate-ping rounded-full bg-emerald-400 opacity-60" /><span className="h-2 w-2 rounded-full bg-emerald-500" /></span>
        <span className="ml-auto text-[11px] text-zinc-400">live · realtime</span>
      </div>
      <div className="grid grid-cols-3 gap-2 sm:grid-cols-6">
        {tiles.map((t) => (
          <div key={t.label} className="min-w-0 rounded-lg bg-zinc-50 px-2 py-1.5 text-center dark:bg-zinc-900">
            <p className={`truncate text-base font-extrabold ${t.tone}`}>{t.value}</p>
            <p className="break-words text-[10px] uppercase leading-tight tracking-wide text-zinc-400">{t.label}</p>
          </div>
        ))}
      </div>
      <div className="mt-3 grid gap-3 md:grid-cols-2">
        <div className="space-y-1 rounded-lg border border-zinc-100 p-2 dark:border-zinc-800">
          <p className="text-xs font-bold">Health checker <span className="font-normal text-zinc-400">· {data.checker.cadence}</span></p>
          <p className="text-xs text-zinc-500">
            Batch {data.checker.batch} oldest-first · ×{data.checker.threads} threads · verify {data.checker.verify_limit} full end-to-end (×{data.checker.verify_threads}) · {data.checker.sweep_timeout_s}s TCP sweep ·
            auto-disable after {data.checker.max_fails} fails ({data.checker.fail_cooldown_h}h account cooldown)
          </p>
          <RunLine label="Last cycle" run={data.last_runs.health_check} />
          <RunLine label="Last auto-disable" run={data.last_runs.auto_disabled} />
        </div>
        <div className="space-y-1 rounded-lg border border-zinc-100 p-2 dark:border-zinc-800">
          <p className="text-xs font-bold">Auto-pool <span className="font-normal text-zinc-400">· refresh {data.pool.refresh_cadence}</span></p>
          <p className="text-xs text-zinc-500">
            Dead auto rows deleted outright (5 straight fails) · never-healthy reaped after {data.pool.stillborn_hours}h ·
            disabled leftovers past {data.pool.purge_after_days}d · manual rows immortal · pool capped at {data.pool.max_auto}
            {data.pool.country ? <> · country lock: {data.pool.country}{data.pool.require_country ? " (required)" : ""}</> : " · no country lock"} ·{" "}
            {c.manual} manual · {c.auto} auto
          </p>
          <RunLine label="Last refresh" run={data.last_runs.pool_refresh} />
          <RunLine label="Last purge" run={data.last_runs.purge} />
        </div>
      </div>
      {data.recent.length > 0 && (
        <div className="mt-3">
          <p className="mb-1 text-xs font-bold">Recent activity</p>
          <div className="space-y-0.5 font-mono text-[11px]">
            {data.recent.map((r, i) => (
              <p key={i} className="truncate text-zinc-500" title={r.message}>
                <span className="text-zinc-400">{timeAgo(r.at)}</span>
                {" · "}
                <span className={r.level === "ERROR" || r.level === "CRITICAL" ? "text-red-500" : r.level === "WARNING" ? "text-amber-500" : ""}>{r.message.slice(0, 120)}</span>
              </p>
            ))}
          </div>
        </div>
      )}
    </Card>
  );
}

export default function ProxiesPage() {
  const qc = useQueryClient();
  const { data, isLoading, isError, refetch } = useProxies();
  const { data: sourceRows } = useProxySources();
  const create = useApiMutation("post", [["proxies"], ["proxy-pipeline"]], "Proxy added");
  const remove = useApiMutation("delete", [["proxies"], ["proxy-pipeline"]]);
  const test = useApiMutation("post", [["proxies"], ["proxy-pipeline"]]);
  const checkAll = useApiMutation("post", [["proxies"], ["proxy-pipeline"]]);
  const refreshPool = useApiMutation("post", [["proxies"], ["proxy-pipeline"], ["proxy-sources"]]);
  const purgePool = useApiMutation("post", [["proxies"], ["proxy-pipeline"]]);
  const resetAll = useApiMutation("post", [["proxies"], ["proxy-pipeline"], ["accounts"]]);
  const createSource = useApiMutation("post", [["proxy-sources"]], "Source added");
  const toggleSource = useApiMutation("put", [["proxy-sources"]], "Source updated");
  const removeSource = useApiMutation("delete", [["proxy-sources"]], "Source deleted");
  const [srcForm, setSrcForm] = useState({ name: "", url: "", default_protocol: "http", default_country: "" });
  const [form, setForm] = useState({ url: "", protocol: "http", username: "", password: "" });
  const [impFile, setImpFile] = useState<File | null>(null);
  const [impProto, setImpProto] = useState("http");
  const [impCountry, setImpCountry] = useState("");
  const [impBusy, setImpBusy] = useState(false);
  const [testBusyId, setTestBusyId] = useState<number | null>(null);
  const [delBusyId, setDelBusyId] = useState<number | null>(null);
  const [impResult, setImpResult] = useState<ProxyImportResult | null>(null);
  const [impError, setImpError] = useState("");
  const proxies = (data ?? []) as Proxy[];
  // Dead/disabled rows are hidden by default — the pool auto-reaps them
  // (stillborn/proven-dead), so the list shows what's actually usable.
  const [filter, setFilter] = useState<"usable" | "new" | "bad" | "off" | "all">("usable");
  const isNew = (p: Proxy) => p.last_checked == null;
  const isBad = (p: Proxy) => !isNew(p) && !p.is_healthy && p.is_active;
  const isOff = (p: Proxy) => !p.is_active;
  const shown = proxies.filter((p) =>
    filter === "all" ? true
    : filter === "new" ? isNew(p)
    : filter === "bad" ? isBad(p)
    : filter === "off" ? isOff(p)
    : p.is_active && (p.is_healthy || isNew(p)),
  );
  const counts = {
    usable: proxies.filter((p) => p.is_active && (p.is_healthy || isNew(p))).length,
    new: proxies.filter(isNew).length,
    bad: proxies.filter(isBad).length,
    off: proxies.filter(isOff).length,
  };

  async function bulkImport() {
    if (!impFile) return;
    setImpBusy(true);
    setImpError("");
    setImpResult(null);
    try {
      const fd = new FormData();
      fd.append("file", impFile);
      fd.append("default_protocol", impProto);
      fd.append("default_country", impCountry);
      const { data } = await api.post("/proxies/import", fd, {
        headers: { "Content-Type": "multipart/form-data" },
        timeout: 120000,
      });
      setImpResult(data as ProxyImportResult);
      setImpFile(null);
      qc.invalidateQueries({ queryKey: ["proxies"] });
      qc.invalidateQueries({ queryKey: ["proxy-pipeline"] });
    } catch (e: unknown) {
      const msg = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Import failed";
      setImpError(String(msg));
    } finally {
      setImpBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="text-xl font-extrabold tracking-tight">Proxy pool</h1>
        <button className="btn-ghost ml-auto !py-1.5 text-xs" disabled={checkAll.isPending} onClick={() => checkAll.mutate({ url: "/proxies/check-all" })}>{checkAll.isPending ? "Checking…" : "Health-check all"}</button>
        <button className="btn-ghost !py-1.5 text-xs" disabled={refreshPool.isPending} onClick={() => refreshPool.mutate({ url: "/proxies/pool/refresh" })}>{refreshPool.isPending ? "Refreshing…" : "Refresh auto-pool"}</button>
        <button className="btn-ghost !py-1.5 text-xs" disabled={purgePool.isPending} onClick={() => { if (confirm("Delete long-dead auto-fetched proxies? Manual ones are never touched.")) purgePool.mutate({ url: "/proxies/pool/purge" }); }}>{purgePool.isPending ? "Purging…" : "Purge stale"}</button>
        <button className="btn-ghost !py-1.5 text-xs !text-red-500" disabled={resetAll.isPending} onClick={() => { if (confirm("RESET ALL proxies?\n\n• Deletes EVERY auto-fetched proxy\n• Zeroes manual proxies (health/stats cleared, re-verify with Health-check)\n• Unlinks ALL accounts from their proxies\n\nSources and pool settings are kept. This cannot be undone.")) resetAll.mutate({ url: "/proxies/reset" }); }}>{resetAll.isPending ? "Resetting…" : "Reset all"}</button>
      </div>
      {(() => {
        const byCountry = new Map<string, number>();
        proxies.forEach((p) => { if (p.country) byCountry.set(p.country, (byCountry.get(p.country) ?? 0) + 1); });
        return byCountry.size > 0 ? (
          <p className="text-xs text-zinc-500">
            {[...byCountry.entries()].map(([c, n]) => `${c}:${n}`).join(" ")}
          </p>
        ) : null;
      })()}
      <PipelineStatus />
      <Card>
        <CardTitle>Add proxy</CardTitle>
        <div className="grid gap-3 md:grid-cols-5">
          <Field label="URL"><input className="input" value={form.url} onChange={(e) => setForm({ ...form, url: e.target.value })} placeholder="http://1.2.3.4:8080" /></Field>
          <Field label="Protocol">
            <select className="input" value={form.protocol} onChange={(e) => setForm({ ...form, protocol: e.target.value })}>
              <option value="http">http</option><option value="socks5">socks5</option><option value="socks4">socks4</option>
            </select>
          </Field>
          <Field label="Username"><input className="input" value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} /></Field>
          <Field label="Password"><input className="input" type="password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} /></Field>
          <div className="flex items-end"><button className="btn-primary w-full" disabled={!form.url || create.isPending} onClick={() => create.mutate({ url: "/proxies", body: { ...form, username: form.username || null, password: form.password || null } }, { onSuccess: () => setForm({ url: "", protocol: "http", username: "", password: "" }) })}>{create.isPending ? "Adding…" : "Add"}</button></div>
        </div>
      </Card>
      <Card>
        <CardTitle>Bulk import (.txt, one per line)</CardTitle>
        <p className="mb-2 text-xs text-zinc-500">
          host:port · scheme://host:port · user:pass@host:port · scheme://user:pass@host:port · host:port:user:pass —
          no-auth (IP-whitelisted) lines work as-is. Optional country tag per line: <code>|DE</code> or <code>#DE</code>.
          Duplicates are skipped, every bad line is reported.
        </p>
        <div className="grid gap-3 md:grid-cols-4">
          <Field label="List file">
            <input type="file" className="input" accept=".txt,text/plain" onChange={(e) => setImpFile(e.target.files?.[0] ?? null)} />
          </Field>
          <Field label="Default protocol">
            <select className="input" value={impProto} onChange={(e) => setImpProto(e.target.value)}>
              <option value="http">http</option><option value="socks5">socks5</option><option value="socks4">socks4</option>
            </select>
          </Field>
          <Field label="Default country (optional)"><input className="input" value={impCountry} onChange={(e) => setImpCountry(e.target.value)} placeholder="DE" maxLength={2} /></Field>
          <div className="flex items-end"><button className="btn-primary w-full" disabled={!impFile || impBusy} onClick={bulkImport}>{impBusy ? "Importing…" : "Import"}</button></div>
        </div>
        {impError && <p className="mt-2 break-words text-sm text-red-500">{impError}</p>}
        {impResult && (
          <div className="mt-2 min-w-0 text-xs">
            <p className="break-words text-emerald-600">Added {impResult.added} · {impResult.duplicates_skipped} duplicates skipped · {impResult.errors.filter((e) => e.reason !== "duplicate").length} bad lines</p>
            {impResult.errors.slice(0, 10).map((e, i) => (
              <p key={i} className="min-w-0 break-all text-zinc-500">line {e.line}: {e.reason} — <code>{e.text}</code></p>
            ))}
          </div>
        )}
      </Card>
      <Card>
        <CardTitle>Auto-pool sources (refreshed every 3h by beat)</CardTitle>
        <p className="mb-2 text-xs text-zinc-500">
          Public lists are low-trust by nature — fetched rows are health-checked like the rest, single-location
          policy applies at insert, and only long-dead auto rows are ever purged. Manual proxies are immortal.
          Pool location is enforced via Settings keys <code>pool_country</code> + <code>pool_require_country</code>.
        </p>
        <div className="grid gap-3 md:grid-cols-5">
          <Field label="Name"><input className="input" value={srcForm.name} onChange={(e) => setSrcForm({ ...srcForm, name: e.target.value })} placeholder="free-list-1" /></Field>
          <Field label="List URL (https txt)"><input className="input" value={srcForm.url} onChange={(e) => setSrcForm({ ...srcForm, url: e.target.value })} placeholder="https://…" /></Field>
          <Field label="Default protocol">
            <select className="input" value={srcForm.default_protocol} onChange={(e) => setSrcForm({ ...srcForm, default_protocol: e.target.value })}>
              <option value="http">http</option><option value="socks5">socks5</option><option value="socks4">socks4</option>
            </select>
          </Field>
          <Field label="Default country"><input className="input" value={srcForm.default_country} onChange={(e) => setSrcForm({ ...srcForm, default_country: e.target.value })} placeholder="DE" maxLength={2} /></Field>
          <div className="flex items-end"><button className="btn-primary w-full" disabled={!srcForm.name || !srcForm.url || createSource.isPending} onClick={() => createSource.mutate({ url: "/proxies/sources", body: srcForm }, { onSuccess: () => setSrcForm({ name: "", url: "", default_protocol: "http", default_country: "" }) })}>{createSource.isPending ? "Adding…" : "Add source"}</button></div>
        </div>
        <div className="mt-2 space-y-1">
          {((sourceRows ?? []) as ProxySource[]).map((s) => (
            <div key={s.id} className="flex min-w-0 flex-wrap items-center gap-2 border-t border-zinc-100 py-1.5 text-sm first:border-0 dark:border-zinc-800">
              <span className={`h-2 w-2 shrink-0 rounded-full ${s.is_active ? "bg-emerald-500" : "bg-zinc-400"}`} />
              <strong title={s.name} className="max-w-[140px] truncate">{s.name}</strong>
              <span title={s.url} className="min-w-0 flex-1 truncate text-xs text-zinc-500">{s.url} · {s.default_protocol}{s.default_country ? ` · ${s.default_country}` : ""}</span>
              <span className="shrink-0 whitespace-nowrap text-xs text-zinc-500">last fetch: +{s.last_added}/{s.last_total}</span>
              <span className="ml-auto flex shrink-0 flex-wrap gap-2">
                <button className="btn-ghost !px-3 !py-1 text-xs" disabled={toggleSource.isPending} onClick={() => toggleSource.mutate({ url: `/proxies/sources/${s.id}`, body: { name: s.name, url: s.url, default_protocol: s.default_protocol, default_country: s.default_country, is_active: !s.is_active } })}>
                  {s.is_active ? "Disable" : "Enable"}
                </button>
                <button className="btn-ghost !px-3 !py-1 text-xs text-red-500" disabled={removeSource.isPending} onClick={() => { if (confirm(`Delete source "${s.name}"? (Its proxies stay.)`)) removeSource.mutate({ url: `/proxies/sources/${s.id}` }); }}>{removeSource.isPending ? "Deleting…" : "Delete"}</button>
              </span>
            </div>
          ))}
        </div>
      </Card>
      {isLoading ? <Spinner /> : isError ? <QueryFailed onRetry={() => refetch()} /> : proxies.length === 0 ? <EmptyState title="No proxies" hint="Assign one proxy per IG account for best deliverability." /> : (
        <Card>
          <div className="mb-1 flex flex-wrap items-center gap-2">
            <CardTitle>Proxies ({shown.length}/{proxies.length})</CardTitle>
            <span className="ml-auto flex flex-wrap justify-end gap-1">
              {(["usable", "new", "bad", "off", "all"] as const).map((f) => (
                <button
                  key={f}
                  onClick={() => setFilter(f)}
                  className={`rounded-full px-2 py-0.5 text-[11px] font-medium ${filter === f ? "bg-emerald-600 text-white" : "bg-zinc-100 text-zinc-500 dark:bg-zinc-800"}`}
                >
                  {f === "usable" ? `Usable ${counts.usable}` : f === "new" ? `New ${counts.new}` : f === "bad" ? `Dead ${counts.bad}` : f === "off" ? `Off ${counts.off}` : `All ${proxies.length}`}
                </button>
              ))}
            </span>
          </div>
          <div className="max-h-[420px] overflow-x-hidden overflow-y-auto pr-1">
          {shown.length === 0 && <p className="py-3 text-center text-xs text-zinc-400">Nothing in this view — try another filter.</p>}
          {shown.map((p) => (
            <div key={p.id} className="flex flex-wrap items-center gap-2 border-t border-zinc-100 py-2 text-sm first:border-0 dark:border-zinc-800">
              <span className={`h-2 w-2 shrink-0 rounded-full ${p.last_checked == null ? "bg-amber-400" : p.is_healthy ? "bg-emerald-500" : "bg-red-500"}`} title={p.last_checked == null ? "new — not checked yet" : undefined} />
              <code className="min-w-0 break-all text-xs">{p.protocol}://{proxyHost(p.url)}</code>
              <span className="w-full text-zinc-500 sm:w-auto">{p.country ?? ""} · {p.latency_ms != null ? `${p.latency_ms}ms` : "—"} · fails {p.fail_count} · {p.source ?? "manual"}</span>
              {!p.is_active && <span className="rounded-full bg-red-100 px-2 py-0.5 text-xs text-red-600 dark:bg-red-900/40">disabled</span>}
              {p.last_error && <span className="min-w-0 max-w-full truncate text-xs text-red-400" title={p.last_error}>· ⚠ {p.last_error.slice(0, 80)}</span>}
              <span className="ml-auto flex gap-2">
                <button className="btn-ghost !px-3 !py-1 text-xs" disabled={testBusyId === p.id} onClick={async () => { setTestBusyId(p.id); try { await test.mutateAsync({ url: `/proxies/${p.id}/test` }); } finally { setTestBusyId(null); } }}>{testBusyId === p.id ? "Testing…" : "Test"}</button>
                <button className="btn-ghost !px-3 !py-1 text-xs text-red-500" disabled={delBusyId === p.id} onClick={async () => { if (!confirm("Delete proxy?")) return; setDelBusyId(p.id); try { await remove.mutateAsync({ url: `/proxies/${p.id}` }); } finally { setDelBusyId(null); } }}>{delBusyId === p.id ? "Deleting…" : "Delete"}</button>
              </span>
            </div>
          ))}
          </div>
          {test.data && <p className="mt-2 break-all text-xs whitespace-pre-wrap text-zinc-500">Last test: {JSON.stringify(test.data)}</p>}
        </Card>
      )}
      <p className="text-xs text-zinc-500">Assign a proxy to an account from the Accounts page (proxy_id). Health checks run every 30 minutes in oldest-first batches (fast TCP sweep, full verify for survivors) so posting never stalls. Results land live via realtime — no page switching needed. Unhealthy auto-fetched proxies are deleted automatically after the retention set in Settings → proxy → pool_purge_after_days (manual ones are never touched).</p>
    </div>
  );
}
