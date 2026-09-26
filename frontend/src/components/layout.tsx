"use client";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import {
  BarChart3, CalendarClock, Captions, Clapperboard, Download, FileText, HeartPulse, Home, Instagram,
  LogOut, Menu, Moon, Music, Server, Settings, SlidersHorizontal, Smartphone, Sun, Users, History,
} from "lucide-react";
import { useTheme } from "next-themes";
import { cn } from "@/lib/utils";
import { useAuth, useUi } from "@/stores/stores";

const NAV = [
  { href: "/dashboard", label: "Dashboard", icon: Home },
  { href: "/dashboard/phone", label: "Phone", icon: Smartphone },
  { href: "/dashboard/videos", label: "Videos", icon: Clapperboard },
  { href: "/dashboard/sources", label: "Sources", icon: Download },
  { href: "/dashboard/accounts", label: "Accounts", icon: Instagram },
  { href: "/dashboard/posts", label: "Posts", icon: History },
  { href: "/dashboard/schedule", label: "Schedule", icon: CalendarClock },
  { href: "/dashboard/captions", label: "Captions", icon: Captions },
  { href: "/dashboard/bios", label: "Profile", icon: FileText },
  { href: "/dashboard/proxies", label: "Proxies", icon: Users },
  { href: "/dashboard/effects", label: "Effects", icon: SlidersHorizontal },
  { href: "/dashboard/audio", label: "Audio", icon: Music },
  { href: "/dashboard/analytics", label: "Analytics", icon: BarChart3 },
  { href: "/dashboard/server", label: "Server", icon: Server },
  { href: "/dashboard/health", label: "Health", icon: HeartPulse },
  { href: "/dashboard/logs", label: "Logs", icon: FileText },
  { href: "/dashboard/settings", label: "Settings", icon: Settings },
];

export function Sidebar() {
  const pathname = usePathname();
  const { sidebarOpen } = useUi();
  const { logout } = useAuth();
  if (!sidebarOpen) return null;
  return (
    <aside className="hidden w-60 shrink-0 flex-col border-r border-zinc-200 bg-white dark:border-zinc-800 dark:bg-zinc-950 md:flex">
      <div className="flex h-16 items-center gap-2 border-b border-zinc-200 px-5 dark:border-zinc-800">
        <Clapperboard className="h-6 w-6 text-emerald-500" />
        <span className="text-lg font-extrabold tracking-tight">IG Funnel</span>
      </div>
      <nav className="flex-1 space-y-1 overflow-y-auto p-3">
        {NAV.map(({ href, label, icon: Icon }) => {
          const active = pathname === href;
          return (
            <Link
              key={href}
              href={href}
              className={cn(
                "flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition",
                active
                  ? "bg-emerald-600/10 text-emerald-600 dark:text-emerald-400"
                  : "text-zinc-600 hover:bg-zinc-100 dark:text-zinc-400 dark:hover:bg-zinc-900"
              )}
            >
              <Icon className="h-4 w-4" />
              {label}
            </Link>
          );
        })}
      </nav>
      <div className="border-t border-zinc-200 p-3 dark:border-zinc-800">
        <button onClick={logout} className="flex w-full items-center gap-3 rounded-lg px-3 py-2 text-sm text-zinc-500 hover:bg-zinc-100 dark:hover:bg-zinc-900">
          <LogOut className="h-4 w-4" /> Log out
        </button>
      </div>
    </aside>
  );
}

export function Header() {
  const { username, logout } = useAuth();
  const { theme, setTheme } = useTheme();
  const router = useRouter();
  return (
    <header className="flex h-16 items-center gap-3 border-b border-zinc-200 bg-white/80 px-4 backdrop-blur dark:border-zinc-800 dark:bg-zinc-950/80 md:hidden">
      <span className="font-extrabold">IG Funnel</span>
      <div className="ml-auto flex min-w-0 items-center gap-2">
        <button onClick={() => setTheme(theme === "dark" ? "light" : "dark")} className="btn-ghost shrink-0 !px-2" aria-label="Toggle theme">
          {theme === "dark" ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
        </button>
        <button onClick={logout} className="btn-ghost shrink-0 !px-2" aria-label="Log out">
          <LogOut className="h-4 w-4" />
        </button>
        <span className="max-w-[120px] truncate text-xs text-zinc-500" title={username ?? ""}>{username}</span>
      </div>
    </header>
  );
}

export function TopBar() {
  const { toggleSidebar, sidebarOpen } = useUi();
  const { username } = useAuth();
  const { theme, setTheme } = useTheme();
  return (
    <header className="sticky top-0 z-10 hidden h-16 items-center gap-3 border-b border-zinc-200 bg-white/80 px-6 backdrop-blur dark:border-zinc-800 dark:bg-zinc-950/80 md:flex">
      <button onClick={toggleSidebar} className="btn-ghost !px-2" aria-label="Toggle sidebar">
        <Menu className="h-5 w-5" />
      </button>
      <span className="text-xs text-zinc-400">{sidebarOpen ? "" : "IG Funnel"}</span>
      <div className="ml-auto flex items-center gap-3">
        <button onClick={() => setTheme(theme === "dark" ? "light" : "dark")} className="btn-ghost !px-2" aria-label="Toggle theme">
          {theme === "dark" ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
        </button>
        <span className="max-w-[200px] truncate rounded-full bg-zinc-100 px-3 py-1 text-xs font-semibold dark:bg-zinc-800" title={username ?? "admin"}>{username ?? "admin"}</span>
      </div>
    </header>
  );
}

/** Mobile bottom nav — same links, horizontally scrollable. */
export function MobileNav() {
  const pathname = usePathname();
  return (
    <nav className="fixed inset-x-0 bottom-0 z-20 flex gap-1 overflow-x-auto border-t border-zinc-200 bg-white p-2 dark:border-zinc-800 dark:bg-zinc-950 md:hidden">
      {NAV.map(({ href, label, icon: Icon }) => (
        <Link
          key={href}
          href={href}
          className={cn(
            "flex shrink-0 flex-col items-center gap-0.5 rounded-lg px-3 py-1.5 text-[10px] font-medium",
            pathname === href ? "text-emerald-500" : "text-zinc-500"
          )}
        >
          <Icon className="h-4 w-4" />
          {label}
        </Link>
      ))}
    </nav>
  );
}
