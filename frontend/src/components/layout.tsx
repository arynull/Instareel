"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { Clapperboard, LogOut, Menu, Search } from "lucide-react";
import { cn } from "@/lib/utils";
import { useAuth, useUi } from "@/stores/stores";
import { NotificationBell } from "@/components/notifications";
import { ThemeToggle } from "@/components/theme-toggle";
import { DisplaySettings } from "@/components/display-settings";
import { openCommandPalette } from "@/components/command-palette";
import { NAV } from "@/components/nav";

export function Sidebar() {
  const pathname = usePathname();
  const { sidebarOpen } = useUi();
  const { logout } = useAuth();
  if (!sidebarOpen) return null;
  return (
    <aside className="surface hidden w-60 shrink-0 flex-col border-r md:flex">
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
  return (
    <header className="surface relative z-30 flex h-16 items-center gap-3 border-b px-4 backdrop-blur md:hidden">
      <span className="font-extrabold">IG Funnel</span>
      <div className="ml-auto flex min-w-0 items-center gap-2">
        <NotificationBell />
        <ThemeToggle />
        <DisplaySettings />
        <button onClick={logout} className="btn-ghost shrink-0 !px-2" aria-label="Log out">
          <LogOut className="h-4 w-4" />
        </button>
        <span className="hidden max-w-[120px] truncate text-xs text-zinc-500 min-[420px]:block" title={username ?? ""}>{username}</span>
      </div>
    </header>
  );
}

export function TopBar() {
  const { toggleSidebar, sidebarOpen } = useUi();
  const { username } = useAuth();
  return (
    <header className="surface sticky top-0 z-10 hidden h-16 items-center gap-3 border-b px-6 backdrop-blur md:flex">
      <button onClick={toggleSidebar} className="btn-ghost !px-2" aria-label="Toggle sidebar">
        <Menu className="h-5 w-5" />
      </button>
      <span className="text-xs text-zinc-400">{sidebarOpen ? "" : "IG Funnel"}</span>
      <div className="ml-auto flex items-center gap-3">
        <button
          onClick={openCommandPalette}
          className="btn-ghost hidden !px-3 !py-1.5 text-xs text-zinc-400 lg:inline-flex"
          aria-label="Open command palette"
        >
          <Search className="h-3.5 w-3.5" />
          <span>Search…</span>
          <kbd className="rounded border border-zinc-300 px-1 text-[10px] dark:border-zinc-600">⌘K</kbd>
        </button>
        <NotificationBell />
        <ThemeToggle />
        <DisplaySettings />
        <span className="max-w-[200px] truncate rounded-full bg-zinc-100 px-3 py-1 text-xs font-semibold dark:bg-zinc-800" title={username ?? "admin"}>{username ?? "admin"}</span>
      </div>
    </header>
  );
}

/** Mobile bottom nav — same links, horizontally scrollable. */
export function MobileNav() {
  const pathname = usePathname();
  return (
    <nav className="surface fixed inset-x-0 bottom-0 z-20 flex gap-1 overflow-x-auto border-t p-2 md:hidden">
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
