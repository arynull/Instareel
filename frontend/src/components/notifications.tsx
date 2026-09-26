"use client";
import { useState } from "react";
import { useRouter } from "next/navigation";
import {
  AlertOctagon, AlertTriangle, Bell, CalendarClock, CheckCheck, CheckCircle2, Info,
} from "lucide-react";
import { cn } from "@/lib/utils";
import {
  useMarkAllNotificationsRead, useMarkNotificationRead, useNotifications,
} from "@/hooks/use-api";

interface NotificationItem {
  id: number;
  type: string;
  severity: "info" | "success" | "warning" | "critical";
  title: string;
  message: string;
  link: string | null;
  read: boolean;
  created_at: string | null;
}

interface UpcomingSlot {
  rule_id: number;
  rule_name: string;
  account: string | null;
  fires_at: string;
  label: string;
  in_seconds: number;
}

const SEVERITY = {
  info: { Icon: Info, dot: "bg-sky-500", text: "text-sky-500" },
  success: { Icon: CheckCircle2, dot: "bg-emerald-500", text: "text-emerald-500" },
  warning: { Icon: AlertTriangle, dot: "bg-amber-500", text: "text-amber-500" },
  critical: { Icon: AlertOctagon, dot: "bg-red-500", text: "text-red-500" },
} as const;

function timeAgo(iso: string | null): string {
  if (!iso) return "";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

function countdown(inSeconds: number): string {
  const s = Math.max(0, inSeconds);
  if (s < 60) return "in <1m";
  if (s < 3600) return `in ${Math.floor(s / 60)}m`;
  if (s < 86400) {
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    return m ? `in ${h}h ${m}m` : `in ${h}h`;
  }
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  return h ? `in ${d}d ${h}h` : `in ${d}d`;
}

export function NotificationBell() {
  const [open, setOpen] = useState(false);
  const router = useRouter();
  const { data } = useNotifications();
  const markRead = useMarkNotificationRead();
  const markAllRead = useMarkAllNotificationsRead();

  const notifications: NotificationItem[] = data?.notifications ?? [];
  const unreadCount: number = data?.unread_count ?? 0;
  const upcoming: UpcomingSlot[] = data?.upcoming ?? [];

  const openItem = (n: NotificationItem) => {
    if (!n.read) markRead.mutate(n.id);
    setOpen(false);
    if (n.link) router.push(n.link);
  };

  return (
    <div className="relative">
      <button
        onClick={() => setOpen((v) => !v)}
        className="btn-ghost relative !px-2"
        aria-label={`Notifications${unreadCount ? ` (${unreadCount} unread)` : ""}`}
      >
        <Bell className="h-4 w-4" />
        {unreadCount > 0 && (
          <span className="absolute -right-0.5 -top-0.5 flex h-4 min-w-4 items-center justify-center rounded-full bg-red-500 px-1 text-[10px] font-bold leading-none text-white">
            {unreadCount > 99 ? "99+" : unreadCount}
          </span>
        )}
      </button>

      {open && (
        <>
          <div className="fixed inset-0 z-30" onClick={() => setOpen(false)} />
          <div className="absolute right-0 top-full z-40 mt-2 flex max-h-[80vh] w-[min(24rem,90vw)] flex-col overflow-hidden rounded-xl border border-zinc-200 bg-white shadow-xl dark:border-zinc-800 dark:bg-zinc-950">
            <div className="flex items-center justify-between border-b border-zinc-200 px-4 py-3 dark:border-zinc-800">
              <span className="text-sm font-bold">Notifications</span>
              {unreadCount > 0 && (
                <button
                  onClick={() => markAllRead.mutate()}
                  className="flex items-center gap-1 text-xs font-medium text-emerald-600 hover:text-emerald-500"
                >
                  <CheckCheck className="h-3.5 w-3.5" /> Mark all read
                </button>
              )}
            </div>

            <div className="flex-1 overflow-y-auto">
              {upcoming.length > 0 && (
                <div className="border-b border-zinc-100 px-4 py-3 dark:border-zinc-800/60">
                  <div className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-zinc-400">
                    Upcoming
                  </div>
                  <ul className="space-y-2">
                    {upcoming.map((u) => (
                      <li key={u.rule_id} className="flex items-center gap-2.5 text-sm">
                        <CalendarClock className="h-4 w-4 shrink-0 text-zinc-400" />
                        <div className="min-w-0 flex-1">
                          <div className="truncate font-medium text-zinc-800 dark:text-zinc-200">
                            {u.rule_name}
                            {u.account && (
                              <span className="font-normal text-zinc-500"> · @{u.account}</span>
                            )}
                          </div>
                          <div className="text-xs text-zinc-500">{u.label}</div>
                        </div>
                        <span className="shrink-0 rounded-full bg-zinc-100 px-2 py-0.5 text-[11px] font-semibold text-zinc-600 dark:bg-zinc-800 dark:text-zinc-300">
                          {countdown(u.in_seconds)}
                        </span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              <div className="px-4 py-3">
                <div className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-zinc-400">
                  Recent events
                </div>
                {notifications.length === 0 ? (
                  <p className="py-6 text-center text-sm text-zinc-400">
                    You&apos;re all caught up — no events yet.
                  </p>
                ) : (
                  <ul className="space-y-1">
                    {notifications.map((n) => {
                      const sev = SEVERITY[n.severity] ?? SEVERITY.info;
                      return (
                        <li key={n.id}>
                          <button
                            onClick={() => openItem(n)}
                            className={cn(
                              "flex w-full items-start gap-2.5 rounded-lg px-2 py-2 text-left transition",
                              n.read ? "opacity-70 hover:bg-zinc-50 dark:hover:bg-zinc-900"
                                : "bg-zinc-50 hover:bg-zinc-100 dark:bg-zinc-900 dark:hover:bg-zinc-800",
                            )}
                          >
                            <sev.Icon className={cn("mt-0.5 h-4 w-4 shrink-0", sev.text)} />
                            <div className="min-w-0 flex-1">
                              <div className="flex items-center gap-2">
                                <span className={cn("truncate text-sm", n.read ? "font-medium" : "font-bold")}>
                                  {n.title}
                                </span>
                                {!n.read && (
                                  <span className={cn("h-2 w-2 shrink-0 rounded-full", sev.dot)} />
                                )}
                              </div>
                              <p className="mt-0.5 line-clamp-2 text-xs text-zinc-500">{n.message}</p>
                              <span className="mt-1 block text-[11px] text-zinc-400">
                                {timeAgo(n.created_at)}
                              </span>
                            </div>
                          </button>
                        </li>
                      );
                    })}
                  </ul>
                )}
              </div>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
