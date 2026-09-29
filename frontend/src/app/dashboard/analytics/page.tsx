"use client";
import { useState } from "react";
import { Area, AreaChart, Bar, BarChart, CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { Card, CardTitle, PageSkeleton, QueryFailed } from "@/components/ui";
import { toast } from "@/components/toast";
import { useAccounts, useBestSlots, useOverview, useApiMutation } from "@/hooks/use-api";
import { api } from "@/lib/api";
import { fmt, parseApiDate, timeAgo } from "@/lib/utils";

/** Tiny sparkline for KPI cards — no axes, just the shape of the trend. */
function Sparkline({ data, dataKey, stroke }: { data: { date: string }[]; dataKey: string; stroke: string }) {
  if (!data || data.length < 2) return null;
  return (
    <div className="mt-2 h-10">
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={data} margin={{ top: 2, right: 0, bottom: 0, left: 0 }}>
          <defs>
            <linearGradient id={`spark-${dataKey}`} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={stroke} stopOpacity={0.35} />
              <stop offset="100%" stopColor={stroke} stopOpacity={0} />
            </linearGradient>
          </defs>
          <Area type="monotone" dataKey={dataKey} stroke={stroke} strokeWidth={1.5} fill={`url(#spark-${dataKey})`} dot={false} isAnimationActive={false} />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

export default function AnalyticsPage() {
  const [days, setDays] = useState(30);
  const [exporting, setExporting] = useState(false);
  const { data, isLoading, isError, refetch } = useOverview(days);
  const { data: accounts } = useAccounts();
  const [slotAccount, setSlotAccount] = useState("");
  const { data: slots, isLoading: slotsLoading } = useBestSlots(slotAccount);
  const refresh = useApiMutation("post", [["overview"]], "Refresh queued — fresh numbers land in a few minutes");

  async function exportCsv() {
    if (exporting) return;
    setExporting(true);
    let url = "";
    try {
      const { data: csv } = await api.get(`/analytics/export`);
      const blob = new Blob([csv], { type: "text/csv" });
      url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "analytics.csv";
      a.click();
      toast("success", "CSV downloaded");
    } catch {
      toast("error", "CSV export failed");
    } finally {
      if (url) URL.revokeObjectURL(url);
      setExporting(false);
    }
  }

  if (isLoading) return <PageSkeleton />;
  if (isError || !data) return <QueryFailed onRetry={() => refetch()} />;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="text-xl font-extrabold tracking-tight">Analytics</h1>
        <select className="input ml-auto !w-auto" value={days} onChange={(e) => setDays(Number(e.target.value))}>
          {[7, 14, 30, 90].map((d) => <option key={d} value={d}>Last {d} days</option>)}
        </select>
        <button className="btn-ghost !py-2 text-xs sm:text-sm" disabled={exporting} onClick={exportCsv}>{exporting ? "Exporting…" : "Export CSV"}</button>
        <button
          className="btn-ghost !py-2 text-xs sm:text-sm"
          disabled={refresh.isPending}
          onClick={() => refresh.mutate({ url: "/analytics/refresh" })}
        >{refresh.isPending ? "Queuing…" : "Refresh now"}</button>
      </div>
      <div className="grid gap-4 sm:grid-cols-3">
        <Card><p className="tnum text-2xl font-extrabold">{fmt(data.total_posts)}</p><p className="text-xs text-zinc-500">Posts</p>
          <Sparkline data={data.series} dataKey="posts" stroke="#38bdf8" /></Card>
        <Card><p className="tnum text-2xl font-extrabold">{fmt(data.total_views)}</p><p className="text-xs text-zinc-500">Views</p>
          <p
            className="mt-1 text-xs text-zinc-400"
            title={data.last_analytics_refresh ? parseApiDate(data.last_analytics_refresh).toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : undefined}
          >
            Last refresh: {data.last_analytics_refresh ? timeAgo(data.last_analytics_refresh) : "never"}
          </p>
          <Sparkline data={data.series} dataKey="views" stroke="#10b981" /></Card>
        <Card><p className="tnum text-2xl font-extrabold">{data.avg_engagement_rate}%</p><p className="text-xs text-zinc-500">Avg engagement</p></Card>
      </div>
      <Card>
        <CardTitle>Views trend</CardTitle>
        <div className="h-64">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={data.series}>
              <CartesianGrid strokeDasharray="3 3" opacity={0.3} />
              <XAxis dataKey="date" tick={{ fontSize: 11 }} minTickGap={30} />
              <YAxis tick={{ fontSize: 11 }} />
              <Tooltip />
              <Line type="monotone" dataKey="views" stroke="#10b981" strokeWidth={2} dot={false} />
            </LineChart>
          </ResponsiveContainer>
        </div>
      </Card>
      <Card>
        <CardTitle>Posts per day</CardTitle>
        <div className="h-56">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={data.series}>
              <CartesianGrid strokeDasharray="3 3" opacity={0.3} />
              <XAxis dataKey="date" tick={{ fontSize: 11 }} minTickGap={30} />
              <YAxis tick={{ fontSize: 11 }} allowDecimals={false} />
              <Tooltip />
              <Bar dataKey="posts" fill="#10b981" radius={[4, 4, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      </Card>
      <Card>
        <CardTitle>Account comparison</CardTitle>
        {(accounts ?? []).length === 0
          ? <p className="text-sm text-zinc-500">No accounts yet.</p>
          : (
            <div className="h-64">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart
                  data={((accounts ?? []) as { id: number; username: string; total_posts: number; total_views: number }[]).map((a) => ({
                    name: `@${a.username}`,
                    posts: a.total_posts ?? 0,
                    views: a.total_views ?? 0,
                  }))}
                  margin={{ top: 4, right: 0, bottom: 0, left: 0 }}
                >
                  <CartesianGrid strokeDasharray="3 3" opacity={0.3} />
                  <XAxis dataKey="name" tick={{ fontSize: 11 }} interval={0} angle={-12} dy={8} height={44} />
                  <YAxis yAxisId="posts" orientation="left" tick={{ fontSize: 11 }} allowDecimals={false} />
                  <YAxis yAxisId="views" orientation="right" tick={{ fontSize: 11 }} tickFormatter={(v: number) => fmt(v)} />
                  <Tooltip formatter={(v: any, name: any) => [fmt(Number(v ?? 0)), name === "views" ? "Views" : "Posts"]} />
                  <Legend wrapperStyle={{ fontSize: 12 }} />
                  <Bar yAxisId="posts" dataKey="posts" name="Posts" fill="#38bdf8" radius={[4, 4, 0, 0]} maxBarSize={42} />
                  <Bar yAxisId="views" dataKey="views" name="Views" fill="#10b981" radius={[4, 4, 0, 0]} maxBarSize={42} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          )}
      </Card>
      <Card>
        <div className="flex flex-wrap items-center gap-2">
          <CardTitle>Best posting slots</CardTitle>
          <select className="input ml-auto !w-auto !py-1 text-xs" value={slotAccount} onChange={(e) => setSlotAccount(e.target.value)}>
            <option value="">Pick an account…</option>
            {((accounts ?? []) as { id: number; username: string }[]).map((a) => (
              <option key={a.id} value={a.id}>@{a.username}</option>
            ))}
          </select>
        </div>
        {!slotAccount ? (
          <p className="mt-1 text-sm text-zinc-500">Select an account to see when its audience watches — learned from your posted history.</p>
        ) : slotsLoading || !slots ? (
          <p className="mt-1 text-sm text-zinc-500">Crunching numbers…</p>
        ) : (slots.slots ?? []).length === 0 ? (
          <p className="mt-1 text-sm text-zinc-500">No posted history yet — post a few reels first.</p>
        ) : (
          <>
            {!slots.personalized && (
              <p className="mt-1 text-xs text-amber-600">Not enough history for @{slots.username} yet — showing global best hours instead.</p>
            )}
            {(slots.slots as { hour_utc: number; hour_local: number; local: string; tz: string; tz_label: string; posts: number; avg_views: number }[]).map((s, i) => (
              <div key={s.hour_utc} className="flex min-w-0 items-center gap-2 border-t border-zinc-100 py-2 text-sm first:border-0 dark:border-zinc-800">
                <span className={`shrink-0 rounded-full px-2 py-0.5 text-xs font-bold ${i === 0 ? "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300" : "bg-zinc-100 text-zinc-500 dark:bg-zinc-800"}`}>
                  {s.local}
                </span>
                <span className="min-w-0 flex-1 truncate text-zinc-500">{s.tz_label} · {s.hour_utc}:00 UTC</span>
                <span className="shrink-0 whitespace-nowrap text-zinc-500">{fmt(s.avg_views)} avg views · {s.posts} posts</span>
              </div>
            ))}
            <p className="mt-1 text-xs text-zinc-500">Set a schedule rule to the top hour ({(slots.slots as { tz_label: string }[])[0]?.tz_label} time) for this account.</p>
          </>
        )}
      </Card>
    </div>
  );
}
