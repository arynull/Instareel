"use client";
import Link from "next/link";
import { Area, AreaChart, Bar, BarChart, CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { Clapperboard, Eye, Heart, Radio, Users } from "lucide-react";
import { Card, CardTitle, EmptyState, PageSkeleton, QueryFailed, StatusBadge } from "@/components/ui";
import { FunnelWidget } from "@/components/dashboard/funnel";
import { HeatmapWidget } from "@/components/dashboard/heatmap";
import { StatusStrip } from "@/components/dashboard/status-strip";
import { useAccounts, useOverview, usePosts, useQueue } from "@/hooks/use-api";
import { fmt, timeAgo } from "@/lib/utils";
import type { Account, Overview, Post } from "@/types/models";

function Kpi({ icon: Icon, label, value, sub }: { icon: typeof Eye; label: string; value: string; sub?: string }) {
  return (
    <Card className="overflow-hidden">
      <div className="flex min-w-0 items-center gap-3">
        <div className="shrink-0 rounded-lg bg-emerald-600/10 p-2.5"><Icon className="h-5 w-5 text-emerald-500" /></div>
        <div className="min-w-0 flex-1">
          <p title={value} className="tnum truncate text-2xl font-extrabold tracking-tight">{value}</p>
          <p className="truncate text-xs font-medium text-zinc-500" title={sub ? `${label} · ${sub}` : label}>{label}{sub ? ` · ${sub}` : ""}</p>
        </div>
      </div>
    </Card>
  );
}

export default function DashboardPage() {
  const { data: overview, isLoading, isError, refetch } = useOverview(30);
  const { data: accounts } = useAccounts();
  const { data: posts } = usePosts();
  const { data: queue } = useQueue();

  if (isLoading) return <PageSkeleton />;
  if (isError || !overview) return <QueryFailed onRetry={() => refetch()} />;
  const ov = overview as Overview;
  const recent = ((posts ?? []) as Post[]).slice(0, 10);

  return (
    <div className="space-y-6">
      <StatusStrip />
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <Kpi icon={Clapperboard} label="Total posts" value={fmt(ov.total_posts)} />
        <Kpi icon={Eye} label="Total views" value={fmt(ov.total_views)} />
        <Kpi icon={Heart} label="Avg engagement" value={`${ov.avg_engagement_rate}%`} />
        <Kpi icon={Users} label="Active accounts" value={String(ov.active_accounts)} sub={`${ov.queue_size} in queue`} />
      </div>

      <div className="grid gap-4 xl:grid-cols-5">
        <div className="xl:col-span-2"><FunnelWidget /></div>
        <div className="xl:col-span-3"><HeatmapWidget /></div>
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        <Card>
          <CardTitle>Views over time (30d)</CardTitle>
          <div className="h-64">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={ov.series}>
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
          <CardTitle>Posts per day (30d)</CardTitle>
          <div className="h-64">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={ov.series}>
                <CartesianGrid strokeDasharray="3 3" opacity={0.3} />
                <XAxis dataKey="date" tick={{ fontSize: 11 }} minTickGap={30} />
                <YAxis tick={{ fontSize: 11 }} allowDecimals={false} />
                <Tooltip />
                <Bar dataKey="posts" fill="#10b981" radius={[4, 4, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </Card>
      </div>

      <div className="grid gap-4 xl:grid-cols-3">
        <Card className="xl:col-span-2">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <CardTitle>Recent posts</CardTitle>
            <Link href="/dashboard/posts" className="text-xs font-semibold text-emerald-500 hover:underline">View all</Link>
          </div>
          {recent.length === 0 ? (
            <EmptyState title="No posts yet" hint="Upload a video and create a schedule rule to get started." />
          ) : (
            <div className="-mx-4 overflow-x-auto px-4 sm:mx-0 sm:px-0">
              <table className="w-full min-w-[640px] text-sm">
                <thead>
                  <tr className="text-left text-xs uppercase text-zinc-400">
                    <th className="py-2 pr-4">Account</th>
                    <th className="py-2 pr-4">Status</th>
                    <th className="py-2 pr-4 text-right">Views</th>
                    <th className="py-2 pr-4 text-right">Likes</th>
                    <th className="py-2 text-right">When</th>
                  </tr>
                </thead>
                <tbody>
                  {recent.map((p) => (
                    <tr key={p.id} className="border-t border-zinc-100 dark:border-zinc-800">
                      <td className="py-2 pr-4 font-medium">#{p.account_id} · video #{p.video_id}</td>
                      <td className="py-2 pr-4"><StatusBadge status={p.status} /></td>
                      <td className="py-2 pr-4 text-right">{fmt(p.views_7d ?? p.views_24h)}</td>
                      <td className="py-2 pr-4 text-right">{fmt(p.likes_24h)}</td>
                      <td className="py-2 text-right text-zinc-500">{timeAgo(p.posted_at ?? p.created_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>

        <div className="space-y-4">
          <Card>
            <CardTitle>Account health</CardTitle>
            <div className="space-y-2">
              {((accounts ?? []) as Account[]).map((a) => (
                <div key={a.id} className="flex min-w-0 items-center gap-2 text-sm">
                  <span className={`h-2 w-2 shrink-0 rounded-full ${a.status === "active" ? "bg-emerald-500" : a.status === "cooldown" ? "bg-amber-500" : "bg-red-500"}`} />
                  <span title={a.username} className="min-w-0 flex-1 truncate font-medium">@{a.username}</span>
                  <span className="shrink-0 whitespace-nowrap text-xs text-zinc-500">{a.posts_today}/{a.max_daily_posts} today</span>
                </div>
              ))}
              {(accounts ?? []).length === 0 && <p className="text-sm text-zinc-500">No accounts yet. <Link href="/dashboard/accounts" className="text-emerald-500 hover:underline">Add one</Link>.</p>}
            </div>
          </Card>
          <Card>
            <CardTitle>Processing queue</CardTitle>
            <div className="flex min-w-0 items-center gap-2 text-sm">
              <Radio className="h-4 w-4 shrink-0 text-emerald-500" />
              <span className="min-w-0 flex-1 break-words"><strong>{ov.queue_size}</strong> videos waiting · <strong>{ov.scheduled_count}</strong> posts scheduled</span>
            </div>
            <Link href="/dashboard/videos/upload" className="btn-primary mt-3 w-full">Upload video</Link>
            {(queue ?? []).length > 0 && (
              <p className="mt-2 text-xs text-zinc-500">Next: {(queue as Post[])[0].scheduled_for ? timeAgo((queue as Post[])[0].scheduled_for) : "asap"}</p>
            )}
          </Card>
        </div>
      </div>

      <Card>
        <CardTitle>Engagement trend (30d)</CardTitle>
        <div className="h-56">
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={ov.series}>
              <CartesianGrid strokeDasharray="3 3" opacity={0.3} />
              <XAxis dataKey="date" tick={{ fontSize: 11 }} minTickGap={30} />
              <YAxis tick={{ fontSize: 11 }} allowDecimals={false} />
              <Tooltip />
              <Area type="monotone" dataKey="posts" stroke="#10b981" fill="#10b981" fillOpacity={0.2} />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      </Card>
    </div>
  );
}
