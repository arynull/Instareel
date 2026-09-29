import {
  BarChart3, CalendarClock, Captions, Clapperboard, Download, FileText, HeartPulse, Home, Instagram,
  Music, Server, Settings, SlidersHorizontal, Smartphone, Users, History,
} from "lucide-react";

/** Single source of truth for dashboard navigation (sidebar, mobile nav, command palette). */
export const NAV = [
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
